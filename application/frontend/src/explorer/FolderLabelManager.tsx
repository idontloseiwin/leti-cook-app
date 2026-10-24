import { useEffect, useRef, useState } from 'react';
import type { ExplorerLabel, Folder } from '../types';
import { deleteExplorerFolder, deleteExplorerLabel, renameExplorerFolder, renameExplorerLabel } from './api';

type Item = { kind: 'folder' | 'label'; id: string; name: string };
type Editing = { item: Item; name: string } | null;

/** Rename or delete folders and labels. Channels always stay: a deleted folder's
 * channels move to no folder, and a deleted label is removed from every channel. */
export function FolderLabelManager({ folders, labels, onChanged }: {
  folders: Folder[]; labels: ExplorerLabel[]; onChanged: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Editing>(null);
  const [confirming, setConfirming] = useState<Item | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  useEffect(() => {
    if (open && dialog.current && !dialog.current.open) dialog.current.showModal();
  }, [open]);
  const close = () => { if (busy) return; dialog.current?.close(); setOpen(false); setEditing(null); setConfirming(null); setMessage(''); };
  const items: Item[] = [
    ...folders.filter((folder) => folder.id).map((folder) => ({ kind: 'folder' as const, id: folder.id!, name: folder.name || 'Untitled folder' })),
    ...labels.map((label) => ({ kind: 'label' as const, id: label.label_key, name: label.name })),
  ];
  const run = async (action: () => Promise<void>, done: string) => {
    setBusy(true); setMessage('');
    try { await action(); setMessage(done); setEditing(null); setConfirming(null); onChanged(); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  const rename = (item: Item, name: string) => run(
    () => item.kind === 'folder' ? renameExplorerFolder(item.id, name) : renameExplorerLabel(item.id, name),
    `${item.kind === 'folder' ? 'Folder' : 'Label'} renamed to ${name}.`);
  const remove = (item: Item) => run(
    () => item.kind === 'folder' ? deleteExplorerFolder(item.id) : deleteExplorerLabel(item.id),
    item.kind === 'folder' ? `${item.name} deleted. Its channels are now in no folder.` : `${item.name} deleted and removed from its channels.`);
  const row = (item: Item) => {
    const key = `${item.kind}:${item.id}`;
    if (editing && editing.item.kind === item.kind && editing.item.id === item.id) {
      return <li key={key}><form onSubmit={(event) => { event.preventDefault(); const name = editing.name.trim(); if (name) void rename(item, name); }}>
        <input aria-label={`New name for ${item.name}`} value={editing.name} maxLength={item.kind === 'folder' ? 80 : 40} autoFocus disabled={busy}
          onChange={(event) => setEditing({ item, name: event.target.value })} />
        <button type="submit" disabled={busy || !editing.name.trim()}>Save</button>
        <button type="button" disabled={busy} onClick={() => setEditing(null)}>Cancel</button></form></li>;
    }
    const sure = confirming && confirming.kind === item.kind && confirming.id === item.id;
    return <li key={key}><span>{item.name}</span>
      <button type="button" disabled={busy} aria-label={`Rename ${item.name}`} onClick={() => { setConfirming(null); setEditing({ item, name: item.name }); }}>Rename</button>
      {sure
        ? <><button type="button" disabled={busy} onClick={() => void remove(item)}>Confirm delete</button>
            <button type="button" disabled={busy} onClick={() => setConfirming(null)}>Keep</button></>
        : <button type="button" disabled={busy} aria-label={`Delete ${item.name}`} onClick={() => { setEditing(null); setConfirming(item); }}>Delete</button>}
    </li>;
  };
  return <>
    <button type="button" className="explorer-secondary-button" onClick={() => setOpen(true)}>Folders and labels</button>
    {open && <dialog ref={dialog} className="saved-work-dialog folder-label-manager" aria-labelledby="folder-label-manager-title"
      onCancel={(event) => { event.preventDefault(); close(); }}>
      <header><h2 id="folder-label-manager-title">Folders and labels</h2>
        <button type="button" aria-label="Close folders and labels" disabled={busy} onClick={close}>×</button></header>
      <p>Renaming changes the name everywhere. Deleting never removes a channel: a folder's channels move to no folder, and a label is taken off every channel.</p>
      <h3>Folders</h3>
      {items.some((item) => item.kind === 'folder') ? <ul>{items.filter((item) => item.kind === 'folder').map(row)}</ul> : <p>No folders yet.</p>}
      <h3>Labels</h3>
      {items.some((item) => item.kind === 'label') ? <ul>{items.filter((item) => item.kind === 'label').map(row)}</ul> : <p>No labels yet.</p>}
      <p role="status" aria-live="polite">{message}</p>
    </dialog>}
  </>;
}
