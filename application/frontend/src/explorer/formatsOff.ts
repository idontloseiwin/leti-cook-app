// A video format the owner turned off in config/config.ini
// ([collection] formats; Streams are off by default). Its stored videos are
// kept but not shown; the server refuses requests for it with format_turned_off.
// The interface learns the list from /research/capabilities (turned_off_video_types).

export type OffFormat = 'long' | 'short' | 'stream';

const NAMES: Record<OffFormat, string> = { long: 'Long-form videos', short: 'Shorts', stream: 'Streams' };

/** The stub heading: "Streams are turned off." */
export function formatTurnedOffTitle(type: OffFormat): string {
  return `${NAMES[type]} are turned off.`;
}

/** One line on where to turn the format on again. */
export function formatTurnedOffHint(type: OffFormat): string {
  return `To show and collect them, add ${type} to formats under [collection] in config/config.ini; `
    + 'stored data appears on the next page load and collection resumes in the next pass.';
}

/** The refusal in words, wherever a request for a turned-off format is answered. */
export const FORMAT_TURNED_OFF_TEXT = 'This video format is turned off in config/config.ini (formats). '
  + 'Its stored videos are kept but not shown; add it to formats to show it again.';

/** Validated `turned_off_video_types` of a capabilities answer; absent means none. */
export function turnedOffTypes(value: unknown): OffFormat[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.some((kind) => kind !== 'long' && kind !== 'short' && kind !== 'stream')
      || new Set(value).size !== value.length || value.length > 2) return null;
  return value as OffFormat[];
}
