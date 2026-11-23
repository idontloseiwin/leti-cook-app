import { useEffect, useRef, useState } from 'react';
import { CollectionTransfer, type CollectionTransferState } from './collectionTransfer';
import { collectionDetailPath, createSavedCollection, loadSavedCollections, mutateCollectionMembers, type SavedCollection } from './savedCollections';
import { savedFailure, savedName, SavedWorkRequestFence } from './savedWork';
import { isAbortError } from './api';
import { videoNoun } from './format';

export function CollectionTransferDialog({ ids, collection, mode = 'add', onClose }: {
  ids: readonly string[]; collection?: SavedCollection; mode?: 'add' | 'remove'; onClose: (result?: CollectionTransferState) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const active = useRef(true);
  const transfer = useRef<CollectionTransfer | undefined>(undefined);
  const [state, setState] = useState<CollectionTransferState>();
  const [targetMode, setTargetMode] = useState<'new' | 'existing'>('new');
  const [name, setName] = useState('');
  const [targetId, setTargetId] = useState('');
  const [collections, setCollections] = useState<SavedCollection[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [listLoaded, setListLoaded] = useState(false);
  const [listBusy, setListBusy] = useState(false);
  const [error, setError] = useState('');
  const listFence = useRef(new SavedWorkRequestFence());
  const listController = useRef<AbortController | undefined>(undefined);
  const load = async (cursor?: string) => {
    const ticket = listFence.current.begin(); if (!ticket) return;
    const controller = new AbortController(); listController.current = controller;
    setListBusy(true); setError('');
    try {
      const page = await loadSavedCollections(cursor, controller.signal);
      if (!active.current || !listFence.current.accepts(ticket)) return;
      setCollections((current) => cursor ? [...current, ...page.collections.filter((row) => !current.some((existing) => existing.id === row.id))] : page.collections);
      setNext(page.next_cursor); setListLoaded(true);
    } catch (failure) { if (active.current && listFence.current.accepts(ticket) && !isAbortError(failure)) setError(savedFailure(failure).message); }
    finally { if (active.current && listFence.current.accepts(ticket)) { listFence.current.finish(ticket); setListBusy(false); } }
  };
  useEffect(() => {
    active.current = true;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => {
      active.current = false; transfer.current?.stopAfterCurrentCommand(); listController.current?.abort(); listFence.current.reset(); dialog.current?.close();
      queueMicrotask(() => { if (!dialog.current?.isConnected && previous?.isConnected) previous.focus({ preventScroll: true }); });
    };
  }, []);
  useEffect(() => { if (targetMode === 'existing' && !listLoaded && !collection) void load(); }, [targetMode]);
  const close = () => { if (!transfer.current?.state.busy) onClose(transfer.current?.state); };
  const submit = async () => {
    if (transfer.current?.state.busy) return;
    try {
      if (!transfer.current) {
        const target = collection ?? (targetMode === 'new' ? { name: savedName(name) } : collections.find((row) => row.id === targetId));
        if (!target) throw new Error('Choose a collection.');
        transfer.current = new CollectionTransfer(ids, target, mode);
      }
      setError('');
      await transfer.current.run({ create: createSavedCollection, mutate: mutateCollectionMembers }, (nextState) => { if (active.current) setState(nextState); });
    } catch (failure) { if (active.current) setError(savedFailure(failure).message); }
  };
  let valid = Boolean(collection || targetId);
  if (!collection && targetMode === 'new') { try { savedName(name); valid = true; } catch { valid = false; } }
  const locked = Boolean(transfer.current), busy = state?.busy ?? false;
  const retry = Boolean(state?.failure && !state.failure.conflict && !state.failure.deleted);
  return <dialog ref={dialog} className="saved-work-dialog collection-transfer-dialog" aria-labelledby="collection-transfer-title"
    onCancel={(event) => { event.preventDefault(); close(); }}>
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <header><h2 id="collection-transfer-title">{mode === 'remove' ? 'Remove from collection' : 'Add to collection'}</h2>
        <button type="button" aria-label="Close collection dialog" disabled={busy} onClick={close}>×</button></header>
      <p>{ids.length.toLocaleString('en')} selected {videoNoun(ids.length)} · {mode === 'remove' ? 'Only collection membership is removed. Videos and channels are unchanged.' : 'Fixed video IDs, not filters or copied statistics.'}</p>
      {ids.length > 500 && <p className="saved-work-hint">Batches are saved independently. Leaving this page stops after the current batch; a partial collection can remain.</p>}
      {collection ? <p><strong>{collection.name}</strong></p> : <fieldset className="collection-target-choice" disabled={locked}>
        <legend>Destination</legend>
        <label><input type="radio" name="collection-target" checked={targetMode === 'new'} onChange={() => setTargetMode('new')} />New collection</label>
        <label><input type="radio" name="collection-target" checked={targetMode === 'existing'} onChange={() => setTargetMode('existing')} />Existing collection</label>
        {targetMode === 'new' ? <label>Name<input value={name} maxLength={240} required autoComplete="off" onChange={(event) => setName(event.target.value)} /></label>
          : <><label>Collection<select value={targetId} onChange={(event) => setTargetId(event.target.value)}>
            <option value="">{listBusy && !listLoaded ? 'Loading collections…' : 'Choose a collection'}</option>
            {collections.map((row) => <option key={row.id} value={row.id}>{row.name} · {row.member_count} {videoNoun(row.member_count)}</option>)}
          </select></label>
          {listLoaded && collections.length === 0 && !error && <p>No collections yet. Create a new one.</p>}
          {error && <button type="button" disabled={listBusy} onClick={() => void load()}>Retry collections</button>}
          {next && <button type="button" disabled={listBusy} onClick={() => void load(next)}>Load more collections</button>}</>}
      </fieldset>}
      {mode === 'add' && <p className="saved-work-hint">Save your selection even when some videos are unavailable. Their current data is checked when you open the collection.</p>}
      {state && <div className="collection-transfer-progress" role="status">
        <p>{state.complete ? (mode === 'remove' ? 'Selection removed from collection.' : 'Selection saved to collection.') : `${state.acknowledged.toLocaleString('en')} of ${state.total.toLocaleString('en')} IDs acknowledged${busy ? ' · Working…' : ''}`}</p>
        {state.collection && !state.complete && <p>The collection exists. Confirmed batches remain; the full selection is not yet confirmed.</p>}
      </div>}
      {(error || state?.failure) && <div role="alert" className="saved-work-error">
        <p>{error || state?.failure?.message}</p>
        {state?.failure?.uncertain && <p>The last batch is not confirmed. Retry sends the same command, without duplicating membership.</p>}
        {(state?.failure?.conflict || state?.failure?.deleted) && <p>Reload the collection before starting another action. Your catalogue selection is kept.</p>}
      </div>}
      <footer>
        {state?.collection && !busy && <a href={collectionDetailPath(state.collection.id)}>Open collection</a>}
        <button type="button" disabled={busy} onClick={close}>{state?.complete ? 'Done' : 'Cancel'}</button>
        {!state?.complete && <button type="submit" className="explorer-primary-button" disabled={busy || !valid || Boolean(state?.failure && !retry)}>
          {busy ? 'Working…' : retry ? 'Retry same command' : mode === 'remove' ? 'Remove selected' : 'Save selection'}
        </button>}
      </footer>
    </form>
  </dialog>;
}
