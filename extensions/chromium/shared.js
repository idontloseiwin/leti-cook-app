/** Syntax only; the backend remains the authority on channel existence. */
export function normalizeAppOrigin(value) {
  if (typeof value !== 'string' || value.length > 128) return null;
  const input = value.trim();
  const match = /^http:\/\/(127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?\/?$/.exec(input);
  if (!match || (match[2] && (Number(match[2]) < 1 || Number(match[2]) > 65535))) return null;
  try { return new URL(input).origin; } catch { return null; }
}

export function permissionPattern(origin) {
  const normalized = normalizeAppOrigin(origin);
  return normalized ? `${normalized}/*` : null;
}

export function normalizeChannelUrl(value) {
  if (typeof value !== 'string' || value.length > 2048 || /[\p{C}\s]/u.test(value)) return null;
  // Do not let URL normalization turn credentials, ports or another host into a locator.
  if (!/^https:\/\/(?:www\.|m\.)?youtube\.com\//.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.username || url.password || url.port) return null;
    const pieces = decodeURIComponent(url.pathname).replace(/\/$/, '').slice(1).split('/');
    const tabs = new Set(['videos', 'shorts', 'streams', 'featured', 'about', 'playlists', 'community']);
    if (pieces[0] === 'channel' && /^UC[A-Za-z0-9_-]{22}$/.test(pieces[1] || '')
        && (pieces.length === 2 || (pieces.length === 3 && tabs.has(pieces[2])))) {
      return `https://www.youtube.com/channel/${pieces[1]}`;
    }
    if (pieces[0]?.startsWith('@') && (pieces.length === 1 || (pieces.length === 2 && tabs.has(pieces[1])))) {
      const handle = pieces[0].slice(1);
      if (Array.from(handle).length < 1 || Array.from(handle).length > 100
          || !/^[\p{L}\p{N}\p{M}_.·-]+$/u.test(handle) || /^[_.·-]|[_.·-]$/.test(handle)) return null;
      return `https://www.youtube.com/@${encodeURIComponent(handle)}`;
    }
  } catch { /* Malformed percent escapes and URLs are not channels. */ }
  return null;
}
