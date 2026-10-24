import { useEffect, useRef, useState, type ReactNode } from 'react';
import { isAbortError } from './api';
import { collectionDetailPath, deleteSavedCollection, loadSavedCollections, renameSavedCollection, type SavedCollection } from './savedCollections';
import { savedFailure, savedName, SavedWorkRequestFence } from './savedWork';
import { formatTimestampLocal } from './localTime';
import { videoNoun } from './format';

export function SavedCollectionsList({ navigation, onNavigate }: { navigation: ReactNode; onNavigate: (path: string) => void }) {
  const [pendingLabel, setPendingLabel] = useState('');
  const [rows, setRows] = useState<SavedCollection[]>([]), [next, setNext] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false), [busy, setBusy] = useState(false), [mustReload, setMustReload] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [editing, setEditing] = useState<{ collection: SavedCollection; name: string }>();
  const fence = useRef(new SavedWorkRequestFence()), controller = useRef<AbortController | undefined>(undefined);
  const load = async (cursor?: string) => {
    const ticket = fence.current.begin(); if (!ticket) return;
    const request = new AbortController(); controller.current = request; setBusy(true); setError('');
    setNotice(''); setPendingLabel(cursor ? 'Loading more collections…' : 'Refreshing collections…');
    try {
      const page = await loadSavedCollections(cursor, request.signal);
      if (!fence.current.accepts(ticket)) return;
      setRows((current) => cursor ? [...current, ...page.collections.filter((row) => !current.some((old) => old.id === row.id))] : page.collections);
      setNext(page.next_cursor); setLoaded(true); setMustReload(false); if (!cursor) setEditing(undefined);
    } catch (failure) { if (fence.current.accepts(ticket) && !isAbortError(failure)) setError(savedFailure(failure).message); }
    finally { if (fence.current.accepts(ticket)) { fence.current.finish(ticket); setBusy(false); } }
  };
  useEffect(() => { void load(); return () => { controller.current?.abort(); fence.current.reset(); }; }, []);
  const mutate = async (collection: SavedCollection, mode: 'rename' | 'delete') => {
    if (busy || mustReload) return;
    if (mode === 'delete' && !window.confirm(`Delete collection “${collection.name}” and its saved membership? Channels and videos are unchanged.`)) return;
    let name = ''; if (mode === 'rename') { try { name = savedName(editing?.name); } catch (failure) { setError(savedFailure(failure).message); return; } }
    const ticket = fence.current.begin(); if (!ticket) return;
    setBusy(true); setError(''); setNotice('');
    setPendingLabel(mode === 'rename' ? 'Saving collection name…' : 'Deleting collection…');
    try {
      if (mode === 'delete') {
        await deleteSavedCollection(collection); if (!fence.current.accepts(ticket)) return;
        setRows((current) => current.filter((row) => row.id !== collection.id)); setNotice('Collection deleted. Channels and videos are unchanged.');
      } else {
        const updated = await renameSavedCollection(collection, name); if (!fence.current.accepts(ticket)) return;
        setRows((current) => current.map((row) => row.id === updated.id ? updated : row)); setEditing(undefined); setNotice('Collection renamed.');
      }
    } catch (failure) { if (fence.current.accepts(ticket)) { setError(`${savedFailure(failure).message} Reload before another change; no change is confirmed.`); setMustReload(true); } }
    finally { if (fence.current.accepts(ticket)) { fence.current.finish(ticket); setBusy(false); } }
  };
  return <main className="saved-views-v2" aria-labelledby="saved-collections-title">
    <header className="saved-views-heading"><h1 id="saved-collections-title">Saved</h1>{navigation}<button type="button" disabled={busy} onClick={() => void load()}>Reload Saved</button></header>
    <p className="saved-work-hint">Collections keep explicit video IDs. Current source values can update; filters never change membership.</p>
    {error && <p role="alert" className="saved-work-error">{error}</p>}<span role="status" className={busy ? 'saved-work-pending' : undefined}>{loaded && busy ? pendingLabel : notice}</span>
    {!loaded && busy && <p role="status">Loading collections…</p>}
    {loaded && !rows.length && !busy && !error && <div className="saved-views-empty"><h2>No collections yet</h2><p>Select videos in a channel and choose Add to collection.</p></div>}
    <ul className="saved-views-list">{rows.map((collection) => <li key={collection.id} className="saved-view-row">
      <div className="saved-view-description"><h2>{collection.name}</h2><p>{collection.member_count.toLocaleString('en')} {videoNoun(collection.member_count)}</p><small>Updated {formatTimestampLocal(collection.updated_at)}</small></div>
      <div className="saved-view-actions"><button type="button" disabled={busy} onClick={() => onNavigate(collectionDetailPath(collection.id))}>Open</button>
        <button type="button" disabled={busy || mustReload} onClick={() => setEditing({ collection, name: collection.name })}>Rename</button>
        <button type="button" disabled={busy || mustReload} onClick={() => void mutate(collection, 'delete')}>Delete</button></div>
      {editing?.collection.id === collection.id && <form className="saved-view-rename" onSubmit={(event) => { event.preventDefault(); void mutate(editing.collection, 'rename'); }}>
        <label htmlFor={`collection-name-${collection.id}`}>Name</label><input id={`collection-name-${collection.id}`} value={editing.name} maxLength={240} autoFocus required disabled={busy || mustReload}
          onChange={(event) => setEditing({ ...editing, name: event.target.value })} />
        <button type="submit" disabled={busy || mustReload}>Save name</button><button type="button" disabled={busy} onClick={() => setEditing(undefined)}>Cancel</button>
      </form>}
    </li>)}</ul>
    {next && <button type="button" disabled={busy || mustReload} onClick={() => void load(next)}>Load more collections</button>}
  </main>;
}
