// Moments (checked, updated, published, observed) are shown in the viewer's own
// time zone, as on the Health page. Publication analysis grouped by UTC hour,
// weekday or calendar stays UTC and says so where it is shown.
const timestampFormatter = new Intl.DateTimeFormat('en', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

const dateFormatter = new Intl.DateTimeFormat('en', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
});

const exactFormatter = new Intl.DateTimeFormat('en', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

function moment(value?: string | null): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatTimestampLocal(
  value?: string | null,
  missing = 'Not recorded',
): string {
  if (!value) return missing;
  const date = moment(value);
  return date ? timestampFormatter.format(date) : 'Invalid timestamp';
}

export function formatDateLocal(value?: string | null): string {
  if (!value) return 'Not reported';
  const date = moment(value);
  return date ? dateFormatter.format(date) : 'Invalid date';
}

/** "2026-09-26 14:05:07" in the viewer's time zone, for exact observation times. */
export function formatExactLocal(value?: string | null, missing = 'Not recorded'): string {
  const date = moment(value);
  if (!date) return missing;
  const part = Object.fromEntries(exactFormatter.formatToParts(date).map(({ type, value: text }) => [type, text]));
  return `${part.year}-${part.month}-${part.day} ${part.hour}:${part.minute}:${part.second}`;
}
