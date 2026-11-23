import { useEffect, useRef, useState } from 'react';
import { currentChannelName, loadDeletedChannels, loadDeletedChannelTitles, removeTrackedChannel, restoreDeletedChannel, type DeletedChannel, type TrashPage, type TrashTitles } from './channelTrashApi';
import type { Channel } from '../types';
import { useSourceExpiry } from '../sourceLifetimeReact';
import './saved.css';
import './channel-trash.css';

export function ChannelTrashDialog({ channelId, channel, apiAvailable = true, onClose, onChanged }: {
  channelId?: string; channel?: Channel; apiAvailable?: boolean; onClose: () => void; onChanged: (removedChannelId?: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null), active = useRef(false), lock = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const [page, setPage] = useState<TrashPage>(), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [now, setNow] = useState(Date.now()), [after, setAfter] = useState<string>();
  const [titles, setTitles] = useState<TrashTitles>(), [titleRefresh, setTitleRefresh] = useState(0);
  useSourceExpiry(titles, () => setTitles(undefined), () => setTitleRefresh(value => value + 1));
  useSourceExpiry(channel, () => setNow(Date.now()));
  useEffect(() => {
    setTitles(undefined);
    if (!page || !apiAvailable || channelId) return;
    const request = new AbortController();
    void loadDeletedChannelTitles(page.items, request.signal).then(value => {
      if (!request.signal.aborted) setTitles(value);
    }).catch(() => { /* Names are optional; the source-free list and Restore stay usable. */ });
    return () => request.abort();
  }, [page, apiAvailable, channelId, titleRefresh]);
  async function run(action: (signal: AbortSignal) => Promise<void>) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    const request = new AbortController(); controller.current = request;
    try { await action(request.signal); }
    catch (failure) { if (active.current) setError(failure instanceof Error && failure.name !== 'AbortError'
      ? failure.message : 'The response was not confirmed. Refresh status before retrying.'); }
    finally { lock.current = false; if (active.current) setBusy(false); }
  }
  const refresh = (cursor?: string) => void run(async (signal) => {
    const result = await loadDeletedChannels(signal, cursor);
    if (active.current) { setPage(result); setAfter(cursor); setNow(Date.now()); }
  });
  useEffect(() => {
    active.current = true; const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal(); if (!channelId) refresh();
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => { active.current = false; controller.current?.abort(); clearInterval(timer); dialog.current?.close();
      queueMicrotask(() => { if (previous?.isConnected) previous.focus(); }); };
  }, []);
  const remove = () => void run(async (signal) => {
    if (!channelId) return; await removeTrackedChannel(channelId, signal);
    if (active.current) onChanged(channelId);
  });
  const restore = (item: DeletedChannel) => void run(async (signal) => {
    await restoreDeletedChannel(item, signal); if (active.current) onChanged();
  });
  return <dialog className="saved-work-dialog channel-trash-dialog" ref={dialog} aria-labelledby="channel-trash-title"
    onKeyDown={(event) => { if (event.key === 'Escape') event.stopPropagation(); }}
    onCancel={(event) => { event.preventDefault(); if (!lock.current) onClose(); }}>
    <header><h2 id="channel-trash-title">{channelId ? 'Move channel to Deleted?' : 'Deleted channels'}</h2>
      <button type="button" disabled={busy} aria-label="Close deleted channels" onClick={onClose}>×</button></header>
    {channelId && <p><strong>{apiAvailable ? currentChannelName(channel) ?? channelId : channelId}</strong><br />{`youtube.com/channel/${channelId}`}</p>}
    <p>Tracking stops while a channel is deleted. Existing data keeps its original 29-day expiry; removal does not extend it.</p>
    <p>You can undo removal for 29 days. Expired history cannot be recovered. After that, automatic cleanup also removes the channel link, labels assigned to it, saved views and linked selections or manual decisions. Shared folders and collections stay.</p>
    {channelId ? <button type="button" disabled={busy} onClick={remove}>Move to Deleted</button> : <>
      <button type="button" disabled={busy} onClick={() => refresh(after)}>Refresh list</button>
      {!page && !error && <p role="status">Loading deleted channels…</p>}
      {page && <ul className="channel-trash-list">{page.items.map((item) => <li key={item.id}>
        {apiAvailable && currentChannelName(titles?.items.find(title => title.id === item.id)) && <strong>{currentChannelName(titles?.items.find(title => title.id === item.id))}</strong>}
        <p>{item.user_input ?? 'Channel link unavailable'}</p>
        <p>{item.restore_until ? `Recovery ends: ${new Date(item.restore_until).toLocaleString()}` : 'Recovery date unavailable.'}</p>
        {item.can_restore && item.restore_until && now < Date.parse(item.restore_until)
          ? <button type="button" disabled={busy} onClick={() => restore(item)}>Restore channel</button>
          : <span>Recovery period ended; awaiting permanent cleanup.</span>}
      </li>)}</ul>}
      {page?.items.length === 0 && <p>No deleted channels on this page.</p>}
      {(after || page?.next_cursor) && <div className="channel-trash-pages">
        <button type="button" disabled={busy || !after} onClick={() => refresh()}>First page</button>
        <button type="button" disabled={busy || !page?.next_cursor} onClick={() => refresh(page?.next_cursor ?? undefined)}>Next page</button>
      </div>}
    </>}
    {busy && <p role="status">Working…</p>}
    {error && <p role="alert" className="saved-work-error">{error}</p>}
    <footer><button type="button" disabled={busy} onClick={onClose}>Close</button></footer>
  </dialog>;
}

export function ChannelTrashControls({ channelId, channel, apiAvailable, onChanged }: { channelId?: string; channel?: Channel; apiAvailable?: boolean; onChanged: (removedChannelId?: string) => void }) {
  const [mode, setMode] = useState<'remove' | 'list'>();
  return <div className="channel-trash-controls">
    {channelId && <button type="button" onClick={() => setMode('remove')}>Remove selected channel</button>}
    <button type="button" onClick={() => setMode('list')}>Deleted channels</button>
    {mode && <ChannelTrashDialog channelId={mode === 'remove' ? channelId : undefined} channel={mode === 'remove' ? channel : undefined} apiAvailable={apiAvailable} onClose={() => setMode(undefined)} onChanged={onChanged} />}
  </div>;
}
