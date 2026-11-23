// Time-zone names and labels for publication analysis; no React or storage.
export const TIME_ZONE_PATTERN = /^(?:UTC|[A-Za-z]+(?:\/[A-Za-z0-9_+-]+){1,2})$/;

/** Short label for headings: "UTC" or "local time". */
export function timeZoneLabel(zone: string): string {
  return zone === 'UTC' ? 'UTC' : 'local time';
}
