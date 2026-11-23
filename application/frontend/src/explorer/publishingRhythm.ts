import type {
  ChannelBaselinePublicationCalendar,
} from '../types';

export type PublicationCalendarCell = {
  date: string | null;
  count: number;
  inYear: boolean;
  week: number;
  weekday: number;
};

export type PublicationCalendarMonth = {
  key: string;
  label: string;
  week: number;
};

export type PublicationCalendarView = {
  year: number;
  cells: PublicationCalendarCell[];
  months: PublicationCalendarMonth[];
  weekCount: number;
  total: number;
  maximum: number;
};

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const CALENDAR_MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Selection identity of a server rhythm (video.publication_rhythm.v1): a new reply resets the ring selection. */
export function publishingRhythmSummaryKey(
  scopeKey: string | undefined,
  summary: { operation_fingerprint: string; evaluated_at: string } | undefined,
): string {
  return JSON.stringify([scopeKey ?? null, summary ? [summary.operation_fingerprint, summary.evaluated_at] : null]);
}

/** True when the calendar carries the days of `year` (the default year, or one read on demand). */
export function calendarYearLoaded(
  calendar: ChannelBaselinePublicationCalendar | undefined,
  year: number,
): boolean {
  return Boolean(calendar?.years.some((entry) => entry.year === year));
}

function utcDateKey(epochMs: number): string {
  return new Date(epochMs).toISOString().slice(0, 10);
}

export function calendarYearOptions(
  calendar: ChannelBaselinePublicationCalendar | undefined,
): number[] {
  return calendar?.available_years.length
    ? [...calendar.available_years]
    : [calendar?.default_year ?? new Date().getUTCFullYear()];
}

export function buildPublicationCalendarView(
  calendar: ChannelBaselinePublicationCalendar | undefined,
  year: number,
): PublicationCalendarView {
  const firstDay = Date.UTC(year, 0, 1);
  const nextYear = Date.UTC(year + 1, 0, 1);
  const dayCount = Math.round((nextYear - firstDay) / DAY_MS);
  const leadingDays = (new Date(firstDay).getUTCDay() + 6) % 7;
  const weekCount = Math.ceil((leadingDays + dayCount) / 7);
  const reportedDays = new Map(
    (calendar?.years.find((entry) => entry.year === year)?.days ?? [])
      .map((day) => [day.date, day.count]),
  );
  const cells = Array.from({ length: weekCount * 7 }, (_, index): PublicationCalendarCell => {
    const dayOffset = index - leadingDays;
    const inYear = dayOffset >= 0 && dayOffset < dayCount;
    const date = inYear ? utcDateKey(firstDay + dayOffset * DAY_MS) : null;
    return {
      date,
      count: date ? reportedDays.get(date) ?? 0 : 0,
      inYear,
      week: Math.floor(index / 7),
      weekday: index % 7,
    };
  });
  const months = CALENDAR_MONTH_NAMES.map((label, month) => {
    const monthStart = Date.UTC(year, month, 1);
    const dayOffset = Math.round((monthStart - firstDay) / DAY_MS);
    return {
      key: `${year}-${String(month + 1).padStart(2, '0')}`,
      label,
      week: Math.floor((leadingDays + dayOffset) / 7),
    };
  });
  return {
    year,
    cells,
    months,
    weekCount,
    total: cells.reduce((sum, cell) => sum + cell.count, 0),
    maximum: cells.reduce((maximum, cell) => Math.max(maximum, cell.count), 0),
  };
}

export function publicationCalendarHeatLevel(count: number, maximum: number): number {
  if (count <= 0 || maximum <= 0) return 0;
  if (maximum === 1) return 4;
  return Math.min(5, Math.max(1, Math.ceil(count / maximum * 5)));
}

export function formatHour(hour: number): string {
  const normalized = ((Math.round(hour) % 24) + 24) % 24;
  return `${String(normalized).padStart(2, '0')}:00`;
}
