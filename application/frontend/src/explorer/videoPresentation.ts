import { safeImageUrl } from './imageHosts';

export function formatVideoCategory(value?: string | null): string {
  return value?.trim() || 'Unknown';
}

export function safeVideoThumbnailUrl(value?: string | null): string | undefined {
  return safeImageUrl(value);
}
