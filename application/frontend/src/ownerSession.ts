let accessMode: string | undefined;

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
        if (!mounted) window.location.replace('/auth');
        return;
      }
      const state: unknown = response.ok ? await response.json() : null;
      if (!state || typeof state !== 'object' || !('authenticated' in state) || state.authenticated !== true) {
        document.getElementById('root')?.replaceChildren();
        document.getElementById('explorer-root')?.replaceChildren();
        window.location.replace('/auth');
        return;
      }
      accessMode = 'access_mode' in state && typeof state.access_mode === 'string' ? state.access_mode : undefined;
      if (!mounted) { mounted = true; mount(); }
    } catch {
      // No new data can pass a failed server check; a navigation returns the
      // real installation error instead of treating network failure as login.
      if (!mounted) window.location.replace('/auth');
    } finally { pending = false; }
  }
  void check();
  window.setInterval(() => { if (!document.hidden) void check(); }, 60_000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void check(); });
  window.addEventListener('pageshow', (event) => { if (event.persisted) void check(); });
}
