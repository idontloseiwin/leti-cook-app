import { useCallback, useEffect, useRef, useState } from 'react';
import { createUserWorkBackup, loadUserWorkStatus, previewUserWorkRestore, restoreUserWorkBackup,
  resolvePendingUserChannel, reconnectAllUserChannels, pollUserWorkStatus, updatePendingUserChannel, UserWorkRequestError } from './userWorkBackupApi';
import { downloadUserWorkBackup, installUserWorkPreferences, readUserWorkFile, resolveUserWorkBookmarks, applyScopedUserWorkPreferences,
  type PendingUserChannel, type UserWorkBackup, type UserWorkStatus, type parseUserWorkPreview } from './userWorkBackup';
import './user-work-backup.css';

type Preview = ReturnType<typeof parseUserWorkPreview>;
const failureMessage = (failure: unknown) => failure instanceof Error && failure.name === 'AbortError'
  ? 'The request stopped before confirmation. Refresh status before retrying.'
  : failure instanceof Error ? failure.message : 'The operation was not confirmed.';

const reconnectProblem: Record<string, string> = {
  needs_input: 'Add the original channel link.', channel_not_found: 'Channel not found. Check its link.',
  unsupported_legacy_channel_link: 'Replace this link with a channel link or handle.',
  provider_key_rejected: 'Check the installation’s YouTube API key, then retry.',
  provider_access_denied: 'Check the installation’s YouTube API access, then retry.',
  channel_add_rate_limited: 'Waiting for the channel-add limit.', provider_quota_exceeded: 'Waiting for YouTube quota.',
  provider_rate_limited: 'Waiting before another YouTube request.', parent_pending: 'Waiting for the parent channel.',
};
function PendingChannelRow({ item, busy, onEdit, onResolve, onDraftChange }: {
  item: PendingUserChannel; busy: boolean;
  onEdit: (item: PendingUserChannel, value: string | null, parent: string | null) => void;
  onResolve: (item: PendingUserChannel) => void;
  onDraftChange: (id: string, changed: boolean) => void;
}) {
  const [value, setValue] = useState(item.user_input ?? '');
  useEffect(() => { setValue(item.user_input ?? ''); onDraftChange(item.id, false); }, [item.id, item.user_input, item.revision, onDraftChange]);
  useEffect(() => () => onDraftChange(item.id, false), [item.id, onDraftChange]);
  const changed = value.trim() !== (item.user_input ?? '');
  const requested = ['queued', 'running', 'waiting'].includes(item.reconnect_state);
  const problem = item.reconnect_error_code ? reconnectProblem[item.reconnect_error_code] : undefined;
  const stateText = item.is_hidden ? 'Hidden channel. Reconnection is excluded.'
    : item.reconnect_state === 'blocked' ? `Needs attention. ${problem ?? 'Check the link and installation settings, then retry.'}`
    : item.reconnect_state === 'running' ? 'Reconnecting channel details…'
    : item.reconnect_state === 'waiting' ? `${problem ?? 'Waiting to retry.'} Next attempt no earlier than ${new Date(item.reconnect_not_before!).toLocaleString()}.`
    : item.reconnect_state === 'queued' ? item.reason === 'parent_pending' ? 'Queued; waiting for its parent channel.' : 'Queued for reconnection.'
    : item.reason === 'needs_input' ? 'Add the original channel link to reconnect this saved item.'
    : item.reason === 'parent_pending' ? 'Its parent must reconnect first. You can queue both together.'
    : 'Your organization is saved. Channel data has not been fetched.';
  return <li className="user-work-pending-row">
    <label>Channel link
      <input value={value} maxLength={2048} disabled={busy} autoComplete="off" placeholder="YouTube channel link or @handle"
        onChange={(event) => { setValue(event.target.value); onDraftChange(item.id, event.target.value.trim() !== (item.user_input ?? '')); }} />
    </label>
    <p>{stateText}</p>
    <div className="user-work-actions">
      <button type="button" disabled={busy || !changed || !value.trim()} onClick={() => onEdit(item, value.trim(), item.parent_ref)}>Save link</button>
      <button type="button" disabled={busy || changed || requested || item.is_hidden || !item.user_input} onClick={() => onResolve(item)}>{item.reconnect_state === 'blocked' ? 'Retry reconnection' : 'Reconnect channel'}</button>
      {item.parent_ref && <button type="button" disabled={busy || changed} onClick={() => onEdit(item, item.user_input, null)}>Remove parent link</button>}
    </div>
  </li>;
}

export function UserWorkBackupDialog({ initialStatus, onClose, onStatus, onPreferencesChanged }: {
  initialStatus: UserWorkStatus; onClose: () => void; onStatus: (status: UserWorkStatus) => void;
  onPreferencesChanged: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const active = useRef(false), lock = useRef(false), controller = useRef<AbortController | null>(null);
  const [status, setStatus] = useState(initialStatus);
  const [pendingOffset, setPendingOffset] = useState(0);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [message, setMessage] = useState('');
  const [backup, setBackup] = useState<UserWorkBackup>(), [preview, setPreview] = useState<Preview>();
  const [restored, setRestored] = useState(false);
  const [drafts, setDrafts] = useState<Set<string>>(() => new Set());
  const onDraftChange = useCallback((id: string, changed: boolean) => setDrafts((current) => {
    if (current.has(id) === changed) return current;
    const next = new Set(current); if (changed) next.add(id); else next.delete(id); return next;
  }), []);
  const hasActiveReconnect = status.reconnect.counts.queued + status.reconnect.counts.running + status.reconnect.counts.waiting > 0;
  useEffect(() => {
    if (busy || !hasActiveReconnect) return;
    return pollUserWorkStatus({ pendingOffset, isBusy: () => lock.current,
      onStatus: (next) => { if (active.current) { setStatus(next); onStatus(next); } },
      onStopped: () => { if (active.current) setError('Automatic status refresh stopped after three failed reads. Use Refresh status. Saved reconnection requests continue.'); },
    });
  }, [busy, hasActiveReconnect, pendingOffset, onStatus]);
  useEffect(() => {
    active.current = true;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => {
      active.current = false; controller.current?.abort(); dialog.current?.close();
      queueMicrotask(() => { if (previous?.isConnected) previous.focus({ preventScroll: true }); });
    };
  }, []);
  const run = async (action: (signal: AbortSignal) => Promise<void>) => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(''); setMessage('');
    const request = new AbortController(); controller.current = request;
    try { await action(request.signal); }
    catch (failure) { if (active.current) setError(failureMessage(failure)); }
    finally { if (active.current) { setBusy(false); lock.current = false; } }
  };
  const refresh = async (signal: AbortSignal, offset = pendingOffset) => {
    const next = await loadUserWorkStatus(signal, offset);
    if (active.current) { setPendingOffset(offset); setStatus(next); onStatus(next); }
    return next;
  };
  const download = () => void run(async (signal) => {
    const result = await createUserWorkBackup(signal);
    if (!active.current) return;
    downloadUserWorkBackup(result.backup);
    const count = result.warnings.reduce((sum, item) => sum + item.count, 0);
    setMessage(count ? `Backup downloaded. ${count} saved references need attention; unresolved links remain in the backup.` : 'Your user-work backup was downloaded.');
  });
  const chooseFile = (file?: File) => {
    setBackup(undefined); setPreview(undefined);
    if (!file) return;
    void run(async (signal) => {
      const document = await readUserWorkFile(file);
      if (signal.aborted || !active.current) return;
      const checked = await previewUserWorkRestore(document, signal);
      if (active.current) { setBackup(checked.backup ?? document); setPreview(checked); }
    });
  };
  const restore = () => {
    if (!backup || !preview?.can_restore || restored) return;
    void run(async (signal) => {
      const result = await restoreUserWorkBackup(backup, signal);
      if (!active.current) return;
      setRestored(true); setPreview(undefined);
      const preferencesSaved = installUserWorkPreferences(result.browser_preferences);
      // Load the restored preferences now: if the status refresh below fails,
      // the workspace must not write its older copy back over them.
      onPreferencesChanged();
      setMessage(preferencesSaved
        ? `Your user work was restored. ${result.pending_count} channels await reconnection. Choose Reconnect all to collect fresh channel data.`
        : 'Your user work was restored, but this browser could not save all display preferences. Keep the backup and retry Apply browser preferences only before reloading.');
      try { await refresh(signal, 0); }
      catch { if (active.current) setError('Restoration succeeded. Status could not refresh; reload the workspace before continuing.'); }
    });
  };
  const applyPreferences = () => {
    if (!backup) return;
    void run(async (signal) => {
      const current = await refresh(signal);
      if (!active.current) return;
      const result = applyScopedUserWorkPreferences(backup.browser_preferences, current);
      if (!result.applied) throw new Error('This browser could not save all preferences. Keep the file and retry Apply browser preferences only; your database was not changed.');
      onStatus(current);
      setMessage(result.ignored_count
        ? `Browser preferences applied. ${result.ignored_count} references were skipped because they are absent from this workspace. The database was not changed.`
        : 'Browser preferences applied. The database was not changed. Reload the workspace to use them.');
    });
  };
  const edit = (item: PendingUserChannel, value: string | null, parent: string | null) => void run(async (signal) => {
    await updatePendingUserChannel(item, value, parent, signal);
    await refresh(signal);
    if (active.current) setMessage('Saved channel link updated. No YouTube data was fetched.');
  });
  const enqueue = (request: (signal: AbortSignal) => Promise<unknown>) => void run(async (signal) => {
    try { await request(signal); }
    catch (failure) {
      // A missing acknowledgement is not proof the write failed. Read once; never resubmit.
      try { await refresh(signal); }
      catch { if (active.current) setError(`${failureMessage(failure)} Status could not refresh. Use Refresh status before another reconnect request.`); return; }
      if (active.current) setError(`${failureMessage(failure)} Status was refreshed; review the queue before another request.`);
      return;
    }
    if (active.current) setMessage('Reconnection request saved. Queued and waiting channels continue after this dialog closes. Initial collection and any manual confirmation appear in the workspace.');
    try { await refresh(signal); }
    catch { if (active.current) setError('Reconnection request was saved, but status could not refresh. Use Refresh status; do not submit it again.'); }
  });
  const resolve = (item: PendingUserChannel) => enqueue((signal) => resolvePendingUserChannel(item, signal));
  const reconnectAll = () => enqueue((signal) => reconnectAllUserChannels(status.reconnect.snapshot, signal));
  return <dialog ref={dialog} className="saved-work-dialog user-work-backup-dialog" aria-labelledby="user-work-backup-title"
    onKeyDown={(event) => { if (event.key === 'Escape') event.stopPropagation(); }}
    onCancel={(event) => { event.preventDefault(); if (!lock.current) onClose(); }}>
    <header><h2 id="user-work-backup-title">Backup and restore</h2>
      <button type="button" aria-label="Close backup dialog" disabled={busy} onClick={onClose}>×</button></header>
    <p>A backup holds only what you entered: channel links, folders, labels, Saved selections, manual decisions and preferences. It holds no YouTube data; statistics, metadata and history are collected again after a restore.</p>
    <section aria-label="Create user-work backup">
      <button type="button" disabled={busy} onClick={download}>Download backup</button>
    </section>
    <section aria-label="Restore user work">
      <label>Choose a user-work backup
        <input type="file" accept="application/json,.json" disabled={busy} onChange={(event) => chooseFile(event.target.files?.[0])} />
      </label>
      <p>Restore into a new installation with no channels, folders or labels yet, for example after moving to another server. It does not roll back an installation in use. Up to 16 MiB; the file is checked before any changes.</p>
      {preview && <div className="user-work-preview">
        <dl>{Object.entries(preview.counts).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value.toLocaleString('en')}</dd></div>)}</dl>
        {!preview.can_restore && <p role="status">{preview.reason === 'workspace_not_empty' ? 'This installation already holds channels, folders or labels, so the backup cannot be restored here. Restore it into a new installation. Nothing was changed.' : 'This workspace cannot accept the backup. Use a clean installation.'}</p>}
        <button type="button" disabled={busy || !preview.can_restore} onClick={restore}>Restore user work</button>
      </div>}
    </section>
    {backup && <section aria-label="Recover browser preferences">
      <p>Replace this browser’s density, bookmarks and collapsed folders using the checked file. Only references already present in this workspace are applied. This can be retried after an interrupted restore.</p>
      <button type="button" disabled={busy} onClick={applyPreferences}>Apply browser preferences only</button>
    </section>}
    <section aria-label="Reconnect restored channels">
      <div className="user-work-actions"><h3>Channels awaiting reconnection</h3>
        <button type="button" disabled={busy} onClick={() => void run(async (signal) => { await refresh(signal); })}>Refresh status</button></div>
      <p>Reconnect all uses fresh YouTube API requests and quota for all pending channels across every page. Hidden channels are excluded. Manual channel-add limits do not apply. If the daily YouTube quota runs out, reconnection resumes automatically after it resets. Old statistics and observations are never restored.</p>
      <p>Not requested: {status.reconnect.counts.idle}. Queued: {status.reconnect.counts.queued}. Reconnecting: {status.reconnect.counts.running}. Waiting: {status.reconnect.counts.waiting}. Needs attention: {status.reconnect.counts.blocked}. Hidden, excluded: {status.reconnect.excluded_hidden}.</p>
      <button type="button" disabled={busy || drafts.size > 0 || status.reconnect.counts.idle === 0} onClick={reconnectAll}>Reconnect all</button>
      {drafts.size > 0 && <p>Save changed links before reconnecting all channels.</p>}
      <p>Closing this dialog keeps saved requests queued. Status refreshes while work is pending and this dialog is open. Initial collection progress and any manual confirmation remain in the workspace.</p>
      {status.pending.length > 0 ? <>
        <ul className="user-work-pending-list">{status.pending.map((item) => <PendingChannelRow key={item.id} item={item} busy={busy} onEdit={edit} onResolve={resolve} onDraftChange={onDraftChange} />)}</ul>
        {status.pending_more && <p>More channels are available on the next page.</p>}
      </> : <p>{pendingOffset ? 'No pending channels on this page.' : 'No channels are awaiting reconnection.'}</p>}
      {(pendingOffset > 0 || status.pending_more) && <div className="user-work-actions" aria-label="Pending channel pages">
        <button type="button" disabled={busy || pendingOffset === 0} onClick={() => void run(async (signal) => { await refresh(signal, Math.max(0, pendingOffset - 100)); })}>Previous 100</button>
        <span>Page {pendingOffset / 100 + 1}</span>
        <button type="button" disabled={busy || !status.pending_more} onClick={() => void run(async (signal) => { await refresh(signal, pendingOffset + 100); })}>Next 100</button>
      </div>}
    </section>
    {busy && <p role="status">Working…</p>}
    {message && <p role="status">{message}</p>}
    {error && <p role="alert" className="saved-work-error">{error}</p>}
    <footer><button type="button" disabled={busy} onClick={onClose}>Close</button>
      <button type="button" disabled={busy} onClick={() => window.location.reload()}>Reload workspace</button></footer>
  </dialog>;
}

/** Server status is the admission check; mounting never fetches provider data. */
export function UserWorkBackupEntry({ onPreferencesChanged }: { onPreferencesChanged: () => void }) {
  const [status, setStatus] = useState<UserWorkStatus>(), [open, setOpen] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [opening, setOpening] = useState(false), [openError, setOpenError] = useState('');
  const admission = useRef<AbortController | null>(null);
  const receive = useCallback((next: UserWorkStatus) => {
    resolveUserWorkBookmarks(next); setStatus(next); onPreferencesChanged();
  }, [onPreferencesChanged]);
  useEffect(() => {
    const controller = new AbortController();
    void loadUserWorkStatus(controller.signal).then((next) => { if (!controller.signal.aborted) receive(next); }).catch((error) => {
      if (!controller.signal.aborted && !(error instanceof UserWorkRequestError && error.status === 404)) setUnavailable(true);
    });
    return () => { controller.abort(); admission.current?.abort(); };
  }, [receive]);
  const openDialog = async () => {
    if (admission.current) return;
    const controller = new AbortController(); admission.current = controller;
    setOpening(true); setOpenError('');
    try {
      // A closed dialog may have left the parent holding another page. Always admit page one.
      const current = await loadUserWorkStatus(controller.signal, 0);
      if (!controller.signal.aborted) { receive(current); setOpen(true); }
    } catch (error) { if (!controller.signal.aborted) setOpenError(failureMessage(error)); }
    finally { if (admission.current === controller) admission.current = null; if (!controller.signal.aborted) setOpening(false); }
  };
  if (!status) return unavailable ? <span className="user-work-unavailable">User-work backup is unavailable. Reload to retry.</span> : null;
  return <>
    <button type="button" className="explorer-secondary-button user-work-backup-trigger" disabled={opening} onClick={() => void openDialog()}>Backup and restore</button>
    {openError && <span role="alert" className="user-work-unavailable">{openError}</span>}
    {open && <UserWorkBackupDialog initialStatus={status} onStatus={receive} onPreferencesChanged={onPreferencesChanged}
      onClose={() => setOpen(false)} />}
  </>;
}
