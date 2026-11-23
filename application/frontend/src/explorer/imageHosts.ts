// Stored thumbnails and channel avatars come only from these hosts. The server's
// Content-Security-Policy (self_hosted_auth.IMAGE_HOSTS) admits the same list.
export const IMAGE_HOSTS: readonly string[] = [
  'img.youtube.com',
  'i.ytimg.com',
  'yt3.ggpht.com',
  'yt3.googleusercontent.com',
];

export function safeImageUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value.trim()) return undefined;
  try {
    const url = new URL(value.trim());
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return undefined;
    return IMAGE_HOSTS.includes(url.hostname) ? url.toString() : undefined;
  } catch {
    return undefined;
  }
}
