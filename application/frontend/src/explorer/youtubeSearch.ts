export type ExplorerYouTubeSearchTarget =
  | { kind: 'channel'; channelId: string }
  | { kind: 'video'; videoId: string }
  | { kind: 'unsupported'; reason: 'handle' | 'youtube_url' };

const YOUTUBE_CHANNEL_ID = /^UC[a-zA-Z0-9_-]{22}$/;
const YOUTUBE_VIDEO_ID = /^[a-zA-Z0-9_-]{11}$/;

function youtubeInputUrl(value: string): URL | undefined {
  let candidate = value.trim();
  if (!candidate) return undefined;
  if (/^(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)(?:[/?#]|$)/i.test(candidate)) {
    candidate = `https://${candidate}`;
  }
  try {
    const parsed = new URL(candidate);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function decodedPathParts(pathname: string): string[] {
  return pathname.split('/').filter(Boolean).map((part) => {
    try {
      return decodeURIComponent(part);
    } catch {
      return part;
    }
  });
}

export function parseExplorerYouTubeSearchTarget(
  value: string,
): ExplorerYouTubeSearchTarget | undefined {
  const trimmed = value.trim();
  if (YOUTUBE_CHANNEL_ID.test(trimmed)) {
    return { kind: 'channel', channelId: trimmed };
  }

  const parsed = youtubeInputUrl(trimmed);
  if (!parsed) return undefined;
  const hostname = parsed.hostname.toLocaleLowerCase();
  const isShortHost = hostname === 'youtu.be' || hostname === 'www.youtu.be';
  const isYouTubeHost = hostname === 'youtube.com' || hostname.endsWith('.youtube.com');
  if (!isShortHost && !isYouTubeHost) return undefined;

  const parts = decodedPathParts(parsed.pathname);
  if (isShortHost) {
    const videoId = parts[0];
    return videoId && YOUTUBE_VIDEO_ID.test(videoId)
      ? { kind: 'video', videoId }
      : { kind: 'unsupported', reason: 'youtube_url' };
  }

  if (parts[0] === 'channel' && parts[1] && YOUTUBE_CHANNEL_ID.test(parts[1])) {
    return { kind: 'channel', channelId: parts[1] };
  }
  if (parts[0]?.startsWith('@')) {
    return { kind: 'unsupported', reason: 'handle' };
  }

  const candidateVideoId = parts[0] === 'watch'
    ? parsed.searchParams.get('v')
    : ['shorts', 'live', 'embed'].includes(parts[0] || '')
      ? parts[1]
      : undefined;
  if (candidateVideoId && YOUTUBE_VIDEO_ID.test(candidateVideoId)) {
    return { kind: 'video', videoId: candidateVideoId };
  }
  return { kind: 'unsupported', reason: 'youtube_url' };
}
