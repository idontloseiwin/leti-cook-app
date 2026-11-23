import { useEffect, useRef, useState } from 'react';
import {
  apiReadAllowed, dismissRuntimeNotice, parseRuntimeState, readDismissedNotices, runtimeNotices,
  type RuntimeState,
} from '../runtimeNotices';
import './runtimeNotices.css';

function browserStorage(): Storage | undefined {
  try { return window.localStorage; } catch { return undefined; }
}

/** Runtime checks never replace the shell or take ownership of a user's form. */
export function RuntimeNotices({ onChange }: { onChange?: (state: RuntimeState) => void }) {
  const [state, setState] = useState<RuntimeState | null>(null);
  const [failed, setFailed] = useState(false);
  const [dismissed, setDismissed] = useState(() => readDismissedNotices(browserStorage()));
  const [recovered, setRecovered] = useState(false);
  const wasBlocked = useRef(false);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let request: AbortController | undefined;
    async function poll() {
      if (!active || request || document.hidden) return;
      clearTimeout(timer);
      const controller = new AbortController();
      request = controller;
      const timeout = setTimeout(() => controller.abort(), 10_000);
      try {
        const response = await fetch('/system/hot-runtime', {
          credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error('runtime_status_unavailable');
        const next = parseRuntimeState(await response.json());
        if (!active) return;
        setState(next); setFailed(false);
        if (!apiReadAllowed(next)) { wasBlocked.current = true; setRecovered(false); }
        else if (wasBlocked.current) setRecovered(true);
        onChangeRef.current?.(next);
      } catch {
        if (active) setFailed(true);
      } finally {
        clearTimeout(timeout); request = undefined;
        if (active) timer = setTimeout(() => void poll(), 5000);
      }
    }
    const visible = () => { if (!document.hidden) void poll(); };
    const storage = () => setDismissed(readDismissedNotices(browserStorage()));
    void poll();
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('storage', storage);
    return () => {
      active = false; clearTimeout(timer); request?.abort();
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('storage', storage);
    };
  }, []);

  const notices = state ? runtimeNotices(state, dismissed).filter(notice => !failed || notice.dismissible) : [];
  if (!notices.length && !failed && !recovered) return null;
  return <div className="runtime-notices" aria-label="Application status">
    {failed && <p className="runtime-notice" role="status">Application status is unavailable. Retrying…</p>}
    {notices.map(notice => <div key={notice.id} className={`runtime-notice runtime-notice-${notice.tone}`} role="status">
      <p>{notice.text}</p>
      {notice.dismissible && <button type="button" aria-label="Dismiss expired records notice" onClick={() => {
        setDismissed(current => dismissRuntimeNotice(notice.id, current, browserStorage()));
      }}>Dismiss</button>}
    </div>)}
    {!failed && recovered && state && apiReadAllowed(state) && <div className="runtime-notice runtime-notice-info" role="status">
      <p>YouTube data is available again.</p>
      <button type="button" onClick={() => window.location.reload()}>Reload data</button>
    </div>}
  </div>;
}
