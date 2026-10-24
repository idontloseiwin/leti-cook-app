/** Serialized by chrome.scripting: deliberately has no imported dependencies.
 * Runs in ISOLATED world on the installation page, never on YouTube.
 * Nothing below reads cookies or returns provider data to the extension.
 */
export async function addChannelInApp(expectedOrigin, channelUrl, lookupJson) {
  let postStarted = false;
  let checkpoint = 'A01';
  const timers = new Set();
  const currentOrigin = () => globalThis.location.origin === expectedOrigin;
  const reply = (status) => lookupJson !== null && ['unavailable', 'rejected'].includes(status)
    ? { status, diagnostic: checkpoint } : { status };
  const record = (value) => value && typeof value === 'object' && !Array.isArray(value);
  async function readJson(response) {
    if (!(response.headers.get('content-type') || '').toLowerCase().includes('application/json')) throw new Error('shape');
    const reader = response.body?.getReader();
    if (!reader) throw new Error('shape');
    let size = 0;
    const chunks = [];
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > (lookupJson !== null ? 4194304 : 65536)) { await reader.cancel(); throw new Error('shape'); }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  }
  async function request(path, init, duration) {
    const phase = path === '/auth/session' ? 0 : path === '/sidebar-user-data' ? 1 : 2;
    checkpoint = `A${phase}1`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), duration);
    timers.add(timer);
    try {
      const response = await fetch(path, { ...init, credentials: 'same-origin', cache: 'no-store',
        redirect: 'error', signal: controller.signal, headers: { Accept: 'application/json', ...(init.headers || {}) } });
      checkpoint = `A${phase}2`;
      const value = await readJson(response);
      checkpoint = `A${phase}3`;
      return { response, value };
    } finally { clearTimeout(timer); timers.delete(timer); }
  }
  try {
    // A missing serialized argument must never turn a passive read into a write.
    checkpoint = 'A00';
    if (lookupJson === undefined) return reply('rejected');
    checkpoint = 'A04';
    if (typeof expectedOrigin !== 'string' || !/^http:\/\/(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?$/.test(expectedOrigin)
        || new URL(expectedOrigin).origin !== expectedOrigin || !currentOrigin()) return reply('rejected');
    // Revalidate the canonical argument inside the destination document as well.
    checkpoint = 'A05';
    if (typeof channelUrl !== 'string' || channelUrl.length > 2048) return reply('rejected');
    const channel = new URL(channelUrl);
    const path = decodeURIComponent(channel.pathname);
    const handle = path.startsWith('/@') ? path.slice(2) : null;
    const validHandle = handle !== null && Array.from(handle).length >= 1 && Array.from(handle).length <= 100
      && /^[\p{L}\p{N}\p{M}_.·-]+$/u.test(handle) && !/^[_.·-]|[_.·-]$/.test(handle);
    if (channel.origin !== 'https://www.youtube.com' || channel.username || channel.password || channel.search || channel.hash
        || !(validHandle || /^\/channel\/UC[A-Za-z0-9_-]{22}$/.test(path))) return reply('rejected');
    const auth = await request('/auth/session', { method: 'GET' }, 10000);
    if (!auth.response.ok || !record(auth.value)) return reply('unavailable');
    if (auth.value.configured === false) return reply('setup_required');
    if (auth.value.authenticated === false) return reply('login_required');
    if (auth.value.configured !== true || auth.value.authenticated !== true
        || !['local', 'password'].includes(auth.value.access_mode)) return reply('unavailable');
    if (!currentOrigin()) return reply('rejected');
    if (lookupJson !== null) {
      // The browser boundary carries primitives only; decode a bounded read
      // request here rather than relying on nested argument-object conversion.
      checkpoint = 'A60';
      if (typeof lookupJson !== 'string' || lookupJson.length > 256) return reply('rejected');
      let lookup;
      try { lookup = JSON.parse(lookupJson); } catch { return reply('rejected'); }
      // Fixed read operations only. No provider lookup and no implicit add/restore.
      checkpoint = 'A61';
      if (!record(lookup) || Object.keys(lookup).sort().join(',') !== 'channelId,videoId') return reply('rejected');
      checkpoint = 'A62';
      if (lookup.channelId !== null && (typeof lookup.channelId !== 'string' || !/^UC[A-Za-z0-9_-]{22}$/.test(lookup.channelId))) return reply('rejected');
      checkpoint = 'A63';
      if (lookup.videoId !== null && (typeof lookup.videoId !== 'string' || !/^[A-Za-z0-9_-]{11}$/.test(lookup.videoId))) return reply('rejected');
      const id = /^\/channel\/(UC[A-Za-z0-9_-]{22})$/.exec(path)?.[1] || lookup.channelId;
      if (id) {
        const { response, value } = await request('/sidebar-user-data', { method: 'GET' }, 10000);
        if (!response.ok || !record(value) || !Array.isArray(value.channels) || value.channels.length > 10000
            || value.channels.some(row => !record(row) || !/^UC[A-Za-z0-9_-]{22}$/.test(row.channel_id))) return reply('unavailable');
        return value.channels.some(row => row.channel_id === id) ? { status: 'exists', channelId: id } : reply('not_tracked');
      }
      if (lookup.videoId) {
        const { response, value } = await request('/explorer/channel-library/video-channel?video_id=' + encodeURIComponent(lookup.videoId), { method: 'GET' }, 10000);
        if (!response.ok || !record(value) || value.contract_version !== 1 || value.video_id !== lookup.videoId) return reply('unavailable');
        if (value.status === 'matched' && /^UC[A-Za-z0-9_-]{22}$/.test(value.channel_id)) return { status: 'exists', channelId: value.channel_id };
        // A missing stored video does not prove that its channel is absent.
        return reply('unknown');
      }
      return reply('unknown');
    }
    postStarted = true;
    const { response, value } = await request('/add-channel', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: channelUrl }) }, 120000);
    const code = record(value) ? (typeof value.code === 'string' ? value.code : record(value.detail) ? value.detail.code : null) : null;
    if (response.status === 401) return reply('login_required');
    if (response.status === 429) return reply('rate_limited');
    if (response.status === 409 && code === 'setup_required') return reply('setup_required');
    if ([400, 403, 404, 409, 413, 422].includes(response.status)) return reply('rejected');
    if (response.status === 503 && ['hot_runtime_not_ready', 'channel_add_admission_unavailable',
      'owner_access_unavailable', 'owner_access_not_configured'].includes(code)) return reply('unavailable');
    if (!response.ok || !record(value)) return reply('unconfirmed');
    if ('error' in value || 'detail' in value) return reply('rejected');
    if (!['added', 'exists', 'restored'].includes(value.status) || !record(value.channel)
        || !/^UC[A-Za-z0-9_-]{22}$/.test(value.channel.channel_id || '')) return reply('unconfirmed');
    const hydration = value.hydration;
    if (hydration !== undefined && (!record(hydration) || hydration.channel_id !== value.channel.channel_id)) return reply('unconfirmed');
    const requiresAttention = hydration !== undefined && (hydration.confirmation_required === true
      || ['confirmation_required', 'error', 'failed', 'partial'].includes(hydration.status)
      || ['confirm', 'retry', 'resume'].includes(hydration.action_required));
    return { status: value.status, channelId: value.channel.channel_id, requiresAttention };
  } catch {
    // The server may have committed before a network/body/timeout failure.
    return reply(postStarted ? 'unconfirmed' : 'unavailable');
  } finally { for (const timer of timers) clearTimeout(timer); }
}
