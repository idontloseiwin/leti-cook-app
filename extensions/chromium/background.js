import { normalizeAppOrigin, normalizeChannelUrl, permissionPattern } from './shared.js';
import { addChannelInApp } from './bridge.js';

const INTENT_KEY = 'channelAddIntent';
const INTENT_LIFETIME_MS = 150000;
const IDENTITY_KEY = 'channelIdentities';
const IDENTITY_LIFETIME_MS = 30 * 60 * 1000;
const STATUSES = new Set(['added', 'exists', 'restored', 'setup_required', 'login_required',
  'busy', 'rate_limited', 'unconfirmed', 'unavailable', 'rejected']);
const SUCCESS = new Set(['added', 'exists', 'restored']);
const READ_DIAGNOSTICS = new Set(['A00', 'A01', 'A02', 'A03', 'A04', 'A05', 'A06', 'A11', 'A12', 'A13', 'A21', 'A22', 'A23', 'A60', 'A61', 'A62', 'A63']);
function invalidReadResult(raw) {
  if (typeof raw === 'string') return 'B95';
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return 'B97';
  if (raw.status === 'rejected') return 'B90';
  if (['added', 'restored'].includes(raw.status)) return 'B91';
  if (raw.status === 'exists') return 'B92';
  if (raw.status === 'unavailable') return 'B93';
  if (!Object.hasOwn(raw, 'status')) return 'B94';
  return 'B96';
}
const youtubeOrigin = (url) => {
  try { return ['https://www.youtube.com', 'https://youtube.com'].includes(new URL(url).origin); }
  catch { return false; }
};
const appTab = (tab, origin) => {
  try {
    const url = new URL(tab.url);
    return url.origin === origin && (url.pathname === '/auth' || url.pathname === '/setup'
      || url.pathname === '/app-explorer-v3' || (url.pathname.startsWith('/app-explorer-v3/')
        && !url.pathname.startsWith('/app-explorer-v3/assets/')));
  } catch { return false; }
};

/** One user command; exported factory makes browser effects explicit in offline tests. */
export function createChannelBridge(api, { now = Date.now, sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)) } = {}) {
  let inFlight = false;
  let readsInFlight = 0;
  let readGeneration = 0;
  const aliases = new Map();
  const reads = new Map();
  let storageReady;
  let identitiesReady;
  let identityWrite = Promise.resolve();
  function trustedStorage() {
    return storageReady ||= Promise.all([
      api.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' }),
      api.storage.session.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' }),
    ]);
  }
  async function ignoreFailure(action) { try { await action(); } catch { /* Result remains truthful. */ } }
  async function loadIdentities() {
    return identitiesReady ||= (async () => {
      const saved = (await api.storage.session.get(IDENTITY_KEY))[IDENTITY_KEY];
      if (!Array.isArray(saved) || saved.length > 100) return;
      for (const row of saved) {
        if (normalizeAppOrigin(row?.origin) !== row?.origin || !row?.origin
            || normalizeChannelUrl(row?.url) !== row?.url || !row?.url
            || !/^UC[A-Za-z0-9_-]{22}$/.test(row.id || '')
            || !Number.isFinite(row.at) || row.at > now() || now() - row.at >= IDENTITY_LIFETIME_MS) continue;
        aliases.set(`${row.origin}|${row.url}`, { id: row.id, at: row.at });
      }
    })().catch(() => {});
  }
  async function rememberIdentity(origin, url, id) {
    await loadIdentities();
    aliases.delete(`${origin}|${url}`);
    aliases.set(`${origin}|${url}`, { id, at: now() });
    for (const [key, row] of aliases) if (now() - row.at >= IDENTITY_LIFETIME_MS) aliases.delete(key);
    while (aliases.size > 100) aliases.delete(aliases.keys().next().value);
    // Identity is a lookup hint, never a persisted claim that tracking is active.
    // Session storage survives worker suspension but not a browser restart.
    const saved = [...aliases].map(([key, row]) => {
      const split = key.indexOf('|');
      return { origin: key.slice(0, split), url: key.slice(split + 1), ...row };
    });
    identityWrite = identityWrite.then(() => api.storage.session.set({ [IDENTITY_KEY]: saved })).catch(() => {});
    await identityWrite;
  }
  async function locate(origin) {
    const tabs = await api.tabs.query({ url: permissionPattern(origin) });
    let tab = tabs.find((candidate) => appTab(candidate, origin));
    if (!tab) tab = await api.tabs.create({ url: `${origin}/app-explorer-v3`, active: false });
    const deadline = now() + 15000;
    while (now() < deadline) {
      tab = await api.tabs.get(tab.id);
      if (tab.status === 'complete') {
        if (!appTab(tab, origin)) throw new Error('destination_changed');
        return tab;
      }
      await sleep(250);
    }
    throw new Error('destination_unavailable');
  }
  async function handle(message, sender) {
    if (message?.type === 'channel-status') return readStatus(message, sender);
    if (!message || typeof message !== 'object' || Array.isArray(message)
        || Object.keys(message).sort().join(',') !== 'channelUrl,type' || message.type !== 'add-channel'
        || sender?.id !== api.runtime.id || sender.frameId !== 0 || !Number.isInteger(sender.tab?.id)
        || !youtubeOrigin(sender.url)) return { status: 'rejected' };
    const channelUrl = normalizeChannelUrl(message.channelUrl);
    if (!channelUrl) return { status: 'rejected' };
    if (inFlight) return { status: 'busy' };
    inFlight = true;
    let dispatched = false;
    let recorded = false;
    try {
      await trustedStorage();
      const prior = (await api.storage.session.get(INTENT_KEY))[INTENT_KEY];
      if (prior) {
        if (Number.isFinite(prior.until) && now() < prior.until) return { status: 'busy' };
        // A restarted worker cannot infer whether the old POST committed.
        // Report that uncertainty once; only another explicit click can retry.
        await api.storage.session.remove(INTENT_KEY);
        return { status: 'unconfirmed' };
      }
      const origin = normalizeAppOrigin((await api.storage.local.get('appOrigin')).appOrigin);
      if (!origin || !await api.permissions.contains({ origins: [permissionPattern(origin)] })) {
        await ignoreFailure(() => api.runtime.openOptionsPage());
        return { status: 'setup_required' };
      }
      const tab = await locate(origin);
      // Recheck after tab selection: permissions/settings can change while waiting.
      if (normalizeAppOrigin((await api.storage.local.get('appOrigin')).appOrigin) !== origin
          || !await api.permissions.contains({ origins: [permissionPattern(origin)] })) return { status: 'setup_required' };
      await api.storage.session.set({ [INTENT_KEY]: { until: now() + INTENT_LIFETIME_MS } });
      recorded = true;
      dispatched = true;
      let timer;
      const results = await Promise.race([
        api.scripting.executeScript({ target: { tabId: tab.id, frameIds: [0] }, world: 'ISOLATED',
          func: addChannelInApp, args: [origin, channelUrl, null] }),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('bridge_timeout')), 140000); }),
      ]).finally(() => clearTimeout(timer));
      const raw = Array.isArray(results) && results.length === 1 && results[0].frameId === 0 ? results[0].result : null;
      let result = raw && STATUSES.has(raw.status) ? { status: raw.status } : { status: 'unconfirmed' };
      if (SUCCESS.has(result.status)) {
        if (!/^UC[A-Za-z0-9_-]{22}$/.test(raw.channelId || '') || typeof raw.requiresAttention !== 'boolean') {
          result = { status: 'unconfirmed' };
        } else {
          result = { status: raw.status, channelId: raw.channelId, requiresAttention: raw.requiresAttention };
          reads.clear();
          readGeneration++;
          await rememberIdentity(origin, channelUrl, raw.channelId);
        }
      }
      if (result.status !== 'unconfirmed') await ignoreFailure(() => api.storage.session.remove(INTENT_KEY));
      if (result.status === 'login_required') {
        await ignoreFailure(() => api.tabs.update(tab.id, { url: `${origin}/auth`, active: true }));
      } else if (result.status === 'setup_required') {
        await ignoreFailure(() => api.tabs.update(tab.id, { url: `${origin}/auth`, active: true }));
      } else if (result.requiresAttention) {
        await ignoreFailure(() => api.tabs.update(tab.id, { active: true }));
      }
      return result;
    } catch {
      if (recorded && !dispatched) await ignoreFailure(() => api.storage.session.remove(INTENT_KEY));
      return { status: dispatched ? 'unconfirmed' : 'unavailable' };
    } finally { inFlight = false; }
  }
  async function readStatus(message, sender) {
    let checkpoint = 'B01';
    if (!message || Object.keys(message).sort().join(',') !== 'channelUrl,type,videoId'
        || sender?.id !== api.runtime.id || sender.frameId !== 0 || !Number.isInteger(sender.tab?.id)
        || !youtubeOrigin(sender.url)) return { status: 'rejected' };
    const channelUrl = normalizeChannelUrl(message.channelUrl);
    if (!channelUrl || (message.videoId !== null && !/^[A-Za-z0-9_-]{11}$/.test(message.videoId))) return { status: 'rejected' };
    if (message.videoId !== null) {
      const page = new URL(sender.url);
      const id = page.pathname === '/watch' ? page.searchParams.get('v') : /^\/(?:shorts|live)\/([^/]+)$/.exec(page.pathname)?.[1];
      if (id !== message.videoId) return { status: 'rejected' };
    }
    if (readsInFlight >= 2) return { status: 'unavailable' };
    readsInFlight++;
    try {
      await trustedStorage();
      checkpoint = 'B02';
      const origin = normalizeAppOrigin((await api.storage.local.get('appOrigin')).appOrigin);
      checkpoint = 'B03';
      if (!origin || !await api.permissions.contains({ origins: [permissionPattern(origin)] })) return { status: 'setup_required' };
      const key = `${origin}|${channelUrl}|${message.videoId || ''}`;
      const prior = reads.get(key);
      if (prior && now() - prior.at < 30000) return prior.result;
      // Passive detection never opens or activates a tab and never opens settings.
      checkpoint = 'B04';
      const tab = (await api.tabs.query({ url: permissionPattern(origin) })).find(candidate => appTab(candidate, origin) && candidate.status === 'complete');
      if (!tab) return { status: 'app_closed' };
      checkpoint = 'B05';
      if (!appTab(await api.tabs.get(tab.id), origin)
          || normalizeAppOrigin((await api.storage.local.get('appOrigin')).appOrigin) !== origin
          || !await api.permissions.contains({ origins: [permissionPattern(origin)] })) return { status: 'unavailable' };
      checkpoint = 'B06';
      await loadIdentities();
      const alias = aliases.get(`${origin}|${channelUrl}`);
      const generation = readGeneration;
      const channelId = alias && now() - alias.at < IDENTITY_LIFETIME_MS ? alias.id : null;
      let timer;
      checkpoint = 'B07';
      const results = await Promise.race([
        api.scripting.executeScript({ target: { tabId: tab.id, frameIds: [0] }, world: 'ISOLATED',
          func: addChannelInApp, args: [origin, channelUrl, JSON.stringify({ channelId, videoId: message.videoId })] }),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('read_timeout')), 25000); }),
      ]).finally(() => clearTimeout(timer));
      checkpoint = 'B08';
      const raw = Array.isArray(results) && results.length === 1 && results[0].frameId === 0 ? results[0].result : null;
      const result = raw?.status === 'exists' && /^UC[A-Za-z0-9_-]{22}$/.test(raw.channelId || '')
        ? { status: 'exists', channelId: raw.channelId }
        : { status: ['not_tracked', 'unknown', 'login_required', 'setup_required'].includes(raw?.status) ? raw.status : 'unavailable' };
      if (result.status === 'unavailable') {
        result.diagnostic = READ_DIAGNOSTICS.has(raw?.diagnostic) ? raw.diagnostic : (raw ? invalidReadResult(raw) : 'B08');
      }
      // A failed lookup must not mask a recovered connection for the cache lifetime.
      if (generation === readGeneration && ['exists', 'not_tracked'].includes(result.status)) reads.set(key, { at: now(), result });
      if (generation === readGeneration && result.status === 'exists' && !channelId) {
        await rememberIdentity(origin, channelUrl, result.channelId);
      }
      if (reads.size > 100) reads.delete(reads.keys().next().value);
      return result;
    } catch { return { status: 'unavailable', diagnostic: checkpoint }; }
    finally { readsInFlight--; }
  }
  return { handle, trustedStorage };
}

if (typeof chrome !== 'undefined' && chrome.runtime?.onMessage) {
  const bridge = createChannelBridge(chrome);
  void bridge.trustedStorage().catch(() => {});
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    void bridge.handle(message, sender).then(reply, () => reply({ status: 'unavailable' }));
    return true;
  });
  chrome.action.onClicked.addListener(() => { void chrome.runtime.openOptionsPage().catch(() => {}); });
}
