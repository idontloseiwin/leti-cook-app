let accessMode: string | undefined;

/** Sent once, just before the page of an ended session is cleared and the browser goes to sign-in: pages keep unsaved
 *  work (the canvas writes this tab's draft) and stop asking before the page is left. */
export const SESSION_ENDED_EVENT = 'leti-cook:session-ended';
// The pages a sign-in may return to: this app's own, by path and query only — never another site, a "//" address or a
// dot segment. auth.js and self_hosted_auth.py check the same pattern.
export const APP_RETURN = /^\/app-explorer-v3(?:\/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*\/?(?:\?[A-Za-z0-9._~%&=+:\/-]*)?$/;
/** The sign-in address that comes back to this page afterwards (this app's pages only; otherwise plain sign-in). */
export function signInAddress(location: { pathname: string; search: string }): string {
  const back = location.pathname + location.search;
  return back.length <= 2048 && APP_RETURN.test(back) ? '/auth?return=' + encodeURIComponent(back) : '/auth';
}

/** The installation's access mode from the last passed check: 'password' or 'local'. */
export function ownerAccessMode(): string | undefined {
  return accessMode;
}

/** Browser admission complements the mandatory server guard; contains no secrets. */
export function mountWithOwnerAccess(mount: () => void) {
  if (import.meta.env.VITE_LIC_DEPLOYMENT_PROFILE !== 'self-hosted') { mount(); return; }
  let pending = false;
  let mounted = false;
  async function check() {
    if (pending) return;
    pending = true;
    try {
      const response = await fetch('/auth/session', { credentials: 'same-origin', cache: 'no-store' });
      // Only a refusal or an explicit signed-out state ends the session. A
      // transient server error keeps the mounted workspace and is retried.
      if (!response.ok && response.status !== 401 && response.status !== 403) {
        if (!mounted) window.location.replace(signInAddress(window.location));
        return;
      }
      const state: unknown = response.ok ? await response.json() : null;
      if (!state || typeof state !== 'object' || !('authenticated' in state) || state.authenticated !== true) {
        if (mounted) try { window.dispatchEvent(new Event(SESSION_ENDED_EVENT)); } catch { /* the page is cleared anyway */ }
        document.getElementById('root')?.replaceChildren();
        document.getElementById('explorer-root')?.replaceChildren();
        // assign, not replace: this history entry (and a canvas's copy in it) stays, and sign-in comes back here.
        window.location.assign(signInAddress(window.location));
        return;
      }
      accessMode = 'access_mode' in state && typeof state.access_mode === 'string' ? state.access_mode : undefined;
      if (!mounted) { mounted = true; mount(); }
    } catch {
      // No new data can pass a failed server check; a navigation returns the
      // real installation error instead of treating network failure as login.
      if (!mounted) window.location.replace(signInAddress(window.location));
    } finally { pending = false; }
  }
  void check();
  window.setInterval(() => { if (!document.hidden) void check(); }, 60_000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void check(); });
  window.addEventListener('pageshow', (event) => { if (event.persisted) void check(); });
}
