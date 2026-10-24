import { useEffect, useRef, useState } from 'react';
import { createSavedView, prepareSavedViewCreate, type SavedView, type SavedViewCreate, type SavedViewSnapshot } from './savedViews';
import { savedFailure, savedName } from './savedWork';

export function SaveViewDialog({ snapshot, onClose, onSaved }: {
  snapshot: SavedViewSnapshot; onClose: () => void; onSaved: (view: SavedView) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const active = useRef(true);
  const inFlight = useRef(false);
  const attempt = useRef<SavedViewCreate | undefined>(undefined);
  const [name, setName] = useState('');
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<ReturnType<typeof savedFailure>>();
  const close = () => { if (!inFlight.current) onClose(); };
  useEffect(() => {
    active.current = true;
    const previousFocus = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    input.current?.focus({ preventScroll: true });
    return () => {
      active.current = false;
      dialog.current?.close();
      queueMicrotask(() => { if (!dialog.current?.isConnected && previousFocus?.isConnected) previousFocus.focus({ preventScroll: true }); });
    };
  }, []);
  const submit = async () => {
    if (inFlight.current) return;
    let command: SavedViewCreate;
    try { command = attempt.current ?? prepareSavedViewCreate(undefined, snapshot, name); }
    catch (error) { setFailure({ ...savedFailure(error), uncertain: false }); return; }
    attempt.current = command;
    inFlight.current = true;
    setPending(true); setFailure(undefined);
    try {
      const saved = await createSavedView(command);
      if (active.current) onSaved(saved);
    } catch (error) {
      if (active.current) setFailure(savedFailure(error));
    } finally {
      inFlight.current = false;
      if (active.current) setPending(false);
    }
  };
  let validName = true;
  try { savedName(name); } catch { validName = false; }
  const retryable = Boolean(attempt.current && failure && !failure.conflict && !failure.deleted);
  return <dialog ref={dialog} className="saved-work-dialog" aria-labelledby="save-view-title"
    onCancel={(event) => { event.preventDefault(); close(); }}>
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <header><h2 id="save-view-title">Save view</h2><button type="button" disabled={pending} onClick={close} aria-label="Close save view">×</button></header>
      <p>{snapshot.channelName || snapshot.channelId}</p>
      <p className="saved-work-hint">Applied filters and sort only. Reopening queries current videos, not a frozen result.</p>
      <label htmlFor="saved-view-name">Name</label>
      <input ref={input} id="saved-view-name" value={name} maxLength={240} autoComplete="off" required
        disabled={pending || Boolean(attempt.current)} onChange={(event) => { setName(event.target.value); setFailure(undefined); }} />
      {failure && <div className="saved-work-error" role="alert">
        <p>{failure.message}</p>
        {failure.uncertain && <p>Save is not confirmed. Retry sends the same request safely. Closing does not undo a save that may have completed; check Saved before starting another.</p>}
        {(failure.conflict || failure.deleted) && <p>Check Saved before creating another view.</p>}
      </div>}
      <footer>
        <button type="button" disabled={pending} onClick={close}>Cancel</button>
        {attempt.current && failure && !failure.uncertain && <button type="button" onClick={() => {
          attempt.current = undefined; setFailure(undefined); input.current?.focus();
        }}>Edit name</button>}
        <button type="submit" className="explorer-primary-button" disabled={pending || !validName || Boolean(attempt.current && failure && !retryable)}>
          {pending ? 'Saving…' : retryable ? 'Retry same save' : 'Save view'}
        </button>
      </footer>
    </form>
  </dialog>;
}
