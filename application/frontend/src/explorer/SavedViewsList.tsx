import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { Channel } from '../types';
import { YouTubeAttribution } from '../components/YouTubeAttribution';
import { isAbortError } from './api';
import { deleteSavedView, loadSavedView, loadSavedViews, renameSavedView, type SavedView } from './savedViews';
import { savedFailure, savedName, SavedWorkRequestFence } from './savedWork';
import { EXPLORER_SORT_LABELS } from './filters';
import { formatTimestampLocal } from './localTime';

export function SavedViewsList({ channels, onOpen, navigation }: { channels: Channel[]; onOpen: (view: SavedView) => void; navigation?: ReactNode }) {
  const [views, setViews] = useState<SavedView[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState('');
  const [mustReload, setMustReload] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const [editing, setEditing] = useState<{ view: SavedView; name: string }>();
  const fence = useRef(new SavedWorkRequestFence());
  const controller = useRef<AbortController | undefined>(undefined);

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
        const incoming = new Map(page.views.map((view) => [view.id, view]));
        const merged = current.map((view) => {
          const next = incoming.get(view.id); incoming.delete(view.id);
          return next && next.revision >= view.revision ? next : view;
        });
        return [...merged, ...incoming.values()];
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
    setBusy(view.id); setError(''); setAnnouncement('');
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
          if (updated.channel_available) onOpen(updated);
          else setError('This channel is unavailable. The saved view is kept.');
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
    {views.length > 0 && <ul className="saved-views-list">{views.map((view) => {
      const channel = channels.find((row) => row.channel_id === view.channel_id);
      const isEditing = editing?.view.id === view.id;
      const sort = view.conditions.filters.sort_by;
      return <li key={view.id} className="saved-view-row">
        <div className="saved-view-description"><h2>{view.name}</h2>
          <p>{channel?.title || view.channel_id}{!view.channel_available && <span> · Channel unavailable</span>}</p>
          <small>Sort: {sort === 'views_gained' ? 'Views gained' : EXPLORER_SORT_LABELS[sort as keyof typeof EXPLORER_SORT_LABELS] || sort} · {view.conditions.filters.sort_order === 'desc' ? 'Descending' : 'Ascending'} · Updated {formatTimestampLocal(view.updated_at)}</small>
        </div>
        <div className="saved-view-actions">
          <button type="button" disabled={Boolean(busy) || !view.channel_available} onClick={() => void act(view, 'open')}>{busy === view.id ? 'Working…' : 'Open'}</button>
          <button type="button" disabled={Boolean(busy) || mustReload} onClick={() => { setEditing({ view, name: view.name }); setError(''); }}>Rename</button>
          <button type="button" disabled={Boolean(busy) || mustReload} onClick={() => void act(view, 'delete')}>Delete</button>
        </div>
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
