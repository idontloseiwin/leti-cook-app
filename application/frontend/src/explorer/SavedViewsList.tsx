import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { Channel } from '../types';
import { YouTubeAttribution } from '../components/YouTubeAttribution';
import { isAbortError, requestJson } from './api';
import {
  UNREADABLE_SAVED_ITEM, deleteSavedView, loadSavedView, loadSavedViews, renameSavedView, savedLibraryTarget,
  type SavedView, type UnreadableSavedView,
} from './savedViews';
import { savedFailure, savedName, SavedWorkRequestFence } from './savedWork';
import { EXPLORER_SORT_LABELS } from './filters';
import { formatTimestampLocal } from './localTime';
import { researchLabels } from './researchQuery';
import { useTimeBasis } from './timeBasis';
import { pageTranslationClient } from './libraryData/conditions.ts';
import { planLibraryOpen } from './libraryData/savedLibrary.ts';
import { loadSavedCollections } from './savedCollections';

// The page's one translation cache, shared with the Library Videos tab.
const translation = pageTranslationClient(requestJson);

/** The sort a row shows: Library's own labels for Library views, field labels for saved queries and studies.
 *  A canvas has no one sort (each branch orders its own rows). */
export function savedViewSort(view: SavedView, origin: string): { label: string; descending: boolean } | null {
  if (view.conditions.version === 4) return null;
  const target = view.conditions.version === 1 ? null : savedLibraryTarget(view, origin);
  const filters = view.conditions.version === 1 ? view.conditions.filters : target?.kind === 'channel' ? target.conditions.filters : null;
  if (filters) return { label: filters.sort_by === 'views_gained' ? 'Views gained' : EXPLORER_SORT_LABELS[filters.sort_by] || filters.sort_by, descending: filters.sort_order === 'desc' };
  const order = view.conditions.version === 1 ? [] : view.conditions.query.order_by;
  return { label: order.map((item) => (researchLabels as Record<string, string>)[item.field] ?? item.field).join(', '), descending: order[0]?.direction === 'desc' };
}

export type SavedSourceName = { id: string; name: string };
type SavedSourceNames = Partial<Record<'folder' | 'label' | 'collection', readonly SavedSourceName[]>>;
type SavedScope = { kind: string; id: string };
/** The scopes a Saved item reads: one query's, or every Source of a canvas. */
const savedScopes = (conditions: SavedView['conditions']): SavedScope[] => conditions.version === 1 ? []
  : conditions.version === 4 ? conditions.graph.nodes.filter((node) => node.type === 'source').map((node) => (node.parameters as { scope: SavedScope }).scope)
  : [conditions.query.scope];

/**
 * The source a row names: the channel's title (or id), else the study's folder,
 * label or collection by name ("Folder · Music"), and the kind alone when that
 * name is unknown or the source was deleted.
 */
export function savedViewSource(view: Pick<SavedView, 'channel_id' | 'conditions'>, channels: Channel[], names: SavedSourceNames): string {
  const channel = channels.find((row) => row.channel_id === view.channel_id);
  if (channel?.title || view.channel_id) return channel?.title || view.channel_id || '';
  const named = savedScopes(view.conditions).map(({ kind, id }) => {
    const name = kind === 'channel' ? channels.find((row) => row.channel_id === id)?.title
      : kind === 'folder' || kind === 'label' || kind === 'collection' ? names[kind]?.find((row) => row.id === id)?.name : undefined;
    const label = kind ? kind[0].toUpperCase() + kind.slice(1) : '';
    return name ? `${label} · ${name}` : label;
  });
  // A canvas names its first two sources and counts the rest.
  return named.length > 2 ? `${named.slice(0, 2).join(', ')} +${named.length - 2}` : named.join(', ');
}

export function SavedViewsList({ channels, folders = [], labels = [], onOpen, navigation }: {
  channels: Channel[]; folders?: readonly SavedSourceName[]; labels?: readonly SavedSourceName[];
  onOpen: (view: SavedView) => void; navigation?: ReactNode;
}) {
  const [views, setViews] = useState<(SavedView | UnreadableSavedView)[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState('');
  const [mustReload, setMustReload] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const [editing, setEditing] = useState<{ view: SavedView; name: string }>();
  // A view whose dates were saved in another zone opens after one note.
  const [pendingOpen, setPendingOpen] = useState<{ view: SavedView; note: string }>();
  const { zone: viewerZone } = useTimeBasis();
  const fence = useRef(new SavedWorkRequestFence());
  const controller = useRef<AbortController | undefined>(undefined);
  // Collection names for studies on a collection: Saved's first page, read only when such a study is listed.
  // A collection beyond it, deleted or unreadable shows as "Collection".
  const [collections, setCollections] = useState<SavedSourceName[]>([]);
  const wantsCollections = views.some((view) => view.kind === 'view' && savedScopes(view.conditions).some((scope) => scope.kind === 'collection'));
  useEffect(() => {
    if (!wantsCollections) return undefined;
    const request = new AbortController();
    loadSavedCollections(undefined, request.signal)
      .then((page) => { if (!request.signal.aborted) setCollections(page.collections.map(({ id, name }) => ({ id, name }))); })
      .catch(() => undefined);
    return () => request.abort();
  }, [wantsCollections]);
  const sourceNames: SavedSourceNames = { folder: folders, label: labels, collection: collections };

  const load = async (cursor?: string) => {
    const ticket = fence.current.begin();
    if (!ticket) return;
    const request = new AbortController(); controller.current = request;
    setBusy(cursor ? 'more' : 'list'); setError('');
    try {
      const page = await loadSavedViews(cursor, request.signal);
      if (!fence.current.accepts(ticket)) return;
      setViews((current) => {
        if (!cursor) return page.views;
        const incoming = new Map(page.views.flatMap((view) => (view.id === null ? [] : [[view.id, view] as const])));
        const merged = current.map((view) => {
          const next = view.id === null ? undefined : incoming.get(view.id);
          if (view.id !== null) incoming.delete(view.id);
          if (!next) return view;
          // The newer read wins; between two readable copies, the higher revision.
          return next.kind === 'unreadable' || view.kind === 'unreadable' || next.revision >= view.revision ? next : view;
        });
        return [...merged, ...incoming.values(), ...page.views.filter((view) => view.id === null)];
      });
      setNextCursor(page.next_cursor); setLoaded(true); setMustReload(false);
      if (!cursor) setEditing(undefined);
    } catch (failure) {
      if (fence.current.accepts(ticket) && !isAbortError(failure)) setError(savedFailure(failure).message);
    } finally {
      if (fence.current.accepts(ticket)) { fence.current.finish(ticket); setBusy(undefined); }
    }
  };
  useEffect(() => {
    void load();
    return () => { controller.current?.abort(); fence.current.reset(); };
  }, []);

  const act = async (view: SavedView, action: 'open' | 'rename' | 'delete') => {
    if (action === 'delete' && !window.confirm(`Delete saved view “${view.name}”? This does not delete its channel or videos.`)) return;
    let name = '';
    if (action === 'rename') {
      try { name = savedName(editing?.name); } catch (failure) { setError(savedFailure(failure).message); return; }
    }
    const ticket = fence.current.begin();
    if (!ticket) return;
    setBusy(view.id); setError(''); setAnnouncement(''); setPendingOpen(undefined);
    try {
      if (action === 'delete') {
        await deleteSavedView(view);
        if (!fence.current.accepts(ticket)) return;
        setViews((current) => current.filter((row) => row.id !== view.id));
        setAnnouncement('Saved view deleted. Channel and videos are unchanged.');
      } else {
        const updated = action === 'open' ? await loadSavedView(view.id) : await renameSavedView(view, name);
        if (!fence.current.accepts(ticket)) return;
        setViews((current) => current.map((row) => row.id === updated.id ? updated : row));
        if (action === 'open') {
          // A catalogue query or research definition opens without its source so
          // the source can be changed; only channel-scoped v1 views need the channel.
          const target = updated.channel_available || updated.conditions.version !== 1 ? savedLibraryTarget(updated, window.location.origin) : null;
          if (!target) setError('This source is unavailable. The saved query is kept.');
          else if (target.kind === 'workshop') onOpen(updated);
          else {
            // Library conditions open through the server translation with the tab's
            // type and the viewer's zone; conditions it cannot run yet open nothing.
            const plan = await planLibraryOpen((...args) => translation.translate(...args), {
              channelId: target.channelId, conditions: target.conditions, videoType: target.videoType, viewerZone, savedZone: target.savedZone,
            });
            if (!fence.current.accepts(ticket)) return;
            if (plan.status === 'blocked') setError(plan.message);
            else if (plan.note) setPendingOpen({ view: updated, note: plan.note });
            else onOpen(updated);
          }
        } else { setEditing(undefined); setAnnouncement('Saved view renamed.'); }
      }
    } catch (failure) {
      if (!fence.current.accepts(ticket)) return;
      const info = savedFailure(failure);
      setError(`${info.message}${action !== 'open' ? ' Change is not confirmed. Reload Saved before making another change.' : ''}`);
      if (action !== 'open' || info.deleted) setMustReload(true);
    } finally {
      if (fence.current.accepts(ticket)) { fence.current.finish(ticket); setBusy(undefined); }
    }
  };
  return <main className="saved-views-v2" aria-labelledby="saved-views-title">
    <header className="saved-views-heading"><div><h1 id="saved-views-title">Saved</h1><p>Saved views · applied filters and sort, ready to reopen.</p></div>
      {navigation}
      <button type="button" disabled={Boolean(busy)} onClick={() => void load()}>Reload Saved</button></header>
    <p className="saved-work-hint">Views query current data. They are not bookmarks, collections or historical snapshots.</p>
    <span className="explorer-sr-only" role="status">{announcement}</span>
    {error && <div role="alert" className="saved-work-error">{error}</div>}
    {!loaded && busy === 'list' && <p role="status">Loading saved views…</p>}
    {loaded && views.length === 0 && !busy && !error && <div className="saved-views-empty"><h2>No saved views yet</h2><p>Open a channel’s Videos, apply filters and choose Save view.</p></div>}
    {views.length > 0 && <ul className="saved-views-list">{views.map((view, index) => {
      // Shown, never opened, renamed or deleted.
      if (view.kind === 'unreadable') return <li key={view.id ?? `unreadable-${index}`} className="saved-view-row">
        <div className="saved-view-description"><h2>{view.name ?? 'Unnamed saved item'}</h2><p>{UNREADABLE_SAVED_ITEM}</p></div>
      </li>;
      const isEditing = editing?.view.id === view.id;
      const sort = savedViewSort(view, window.location.origin);
      return <li key={view.id} className="saved-view-row">
        <div className="saved-view-description"><h2>{view.name}</h2>
          <p>{savedViewSource(view, channels, sourceNames)}{!view.channel_available && <span> · Source unavailable</span>}</p>
          <small>{sort ? `Sort: ${sort.label} · ${sort.descending ? 'Descending' : 'Ascending'}` : 'Research canvas'} · Updated {formatTimestampLocal(view.updated_at)}</small>
        </div>
        <div className="saved-view-actions">
          <button type="button" disabled={Boolean(busy) || (!view.channel_available && view.conditions.version === 1)} onClick={() => void act(view, 'open')}>{busy === view.id ? 'Working…' : 'Open'}</button>
          <button type="button" disabled={Boolean(busy) || mustReload} onClick={() => { setEditing({ view, name: view.name }); setError(''); }}>Rename</button>
          <button type="button" disabled={Boolean(busy) || mustReload} onClick={() => void act(view, 'delete')}>Delete</button>
        </div>
        {pendingOpen?.view.id === view.id && <div className="saved-view-rename saved-view-zone-note" role="status">
          <span>{pendingOpen.note}</span>
          <button type="button" disabled={Boolean(busy)} onClick={() => { const next = pendingOpen.view; setPendingOpen(undefined); onOpen(next); }}>Open view</button>
          <button type="button" onClick={() => setPendingOpen(undefined)}>Cancel</button>
        </div>}
        {isEditing && <form className="saved-view-rename" onSubmit={(event) => { event.preventDefault(); void act(editing.view, 'rename'); }}>
          <label htmlFor={`rename-${view.id}`}>View name</label><input id={`rename-${view.id}`} value={editing.name} maxLength={240} required autoFocus disabled={Boolean(busy) || mustReload}
            onChange={(event) => setEditing({ ...editing, name: event.target.value })} />
          <button type="submit" disabled={Boolean(busy) || mustReload}>Save name</button><button type="button" disabled={Boolean(busy)} onClick={() => setEditing(undefined)}>Cancel</button>
        </form>}
      </li>;
    })}</ul>}
    {nextCursor && <button type="button" className="saved-views-more" disabled={Boolean(busy) || mustReload} onClick={() => void load(nextCursor)}>{busy === 'more' ? 'Loading…' : 'Load more saved views'}</button>}
    <footer><YouTubeAttribution label="Open YouTube" /></footer>
  </main>;
}
