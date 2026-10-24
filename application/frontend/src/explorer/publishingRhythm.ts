import type {
  ChannelBaselinePublicationCalendar,
  ChannelBaselinePublicationPoint,
} from '../types';
import { zonedDayStart, zonedParts } from './timeZones';

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

export type PublishingObservation = {
  key: string;
  videoId: string;
  title: string | null;
  publishedAt: string;
  epochMs: number;
  year: number;
  weekday: number;
  hour: number;
  minute: number;
  previousGapMs: number | null;
};

export type PublishingMonth = {
  key: string;
  year: number;
  month: number;
  count: number;
  hasLongPause: boolean;
};

export type PublishingRhythmData = {
  observations: PublishingObservation[];
  byHour: number[];
  byWeekday: number[];
  grid: number[][];
  medianGapMs: number | null;
  maxGapMs: number | null;
  peakHour: number | null;
  peakWeekday: number | null;
  months: PublishingMonth[];
};

export type PublishingWindow = {
  startHour: number;
  endHour: number;
  durationHours: number;
  count: number;
  share: number;
};

export type EdgeYearSlotComparison = {
  fromYear: number;
  toYear: number;
  hours: number;
  fromCount: number;
  toCount: number;
};

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const LONG_PAUSE_MS = 21 * DAY_MS;
const MIN_EDGE_YEAR_SLOT_OBSERVATIONS = 3;
const CALENDAR_MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function publishingRhythmSelectionKey(
  scopeKey: string | undefined,
  publicationPoints: readonly ChannelBaselinePublicationPoint[] | undefined,
): string {
  return JSON.stringify([
    scopeKey ?? null,
    publicationPoints === undefined
      ? null
      : publicationPoints.map((point) => [point.video_id, point.published_at]),
  ]);
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

function median(values: readonly number[]): number | null {
  if (!values.length) return null;
  const ordered = [...values].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2
    ? ordered[middle]
    : (ordered[middle - 1] + ordered[middle]) / 2;
}

function monthKey(year: number, month: number): string {
  return `${year}-${String(month + 1).padStart(2, '0')}`;
}

function peakGridCell(grid: readonly (readonly number[])[]): { weekday: number; hour: number } | null {
  let peak: { weekday: number; hour: number; count: number } | null = null;
  grid.forEach((hours, weekday) => hours.forEach((count, hour) => {
    if (count > 0 && (peak === null || count > peak.count)) peak = { weekday, hour, count };
  }));
  return peak;
}

/** Hours, weekdays, months and years are counted in `zone`; gaps are zone-free. */
export function buildPublishingRhythm(
  points: readonly ChannelBaselinePublicationPoint[],
  zone = 'UTC',
): PublishingRhythmData {
  const ordered = points.flatMap((point, originalIndex) => {
    const epochMs = Date.parse(point.published_at);
    if (!Number.isFinite(epochMs)) return [];
    const parts = zonedParts(epochMs, zone);
    return [{
      originalIndex,
      videoId: point.video_id,
      title: point.title,
      publishedAt: point.published_at,
      epochMs,
      year: parts.year,
      month: parts.month,
      weekday: parts.weekday,
      hour: parts.hour,
      minute: parts.minute,
    }];
  }).sort((left, right) => (
    left.epochMs - right.epochMs
    || left.videoId.localeCompare(right.videoId)
    || left.originalIndex - right.originalIndex
  ));

  const observations: PublishingObservation[] = ordered.map((point, index) => ({
    key: `${point.videoId}:${point.epochMs}:${point.originalIndex}`,
    videoId: point.videoId,
    title: point.title,
    publishedAt: point.publishedAt,
    epochMs: point.epochMs,
    year: point.year,
    weekday: point.weekday,
    hour: point.hour,
    minute: point.minute,
    previousGapMs: index === 0 ? null : point.epochMs - ordered[index - 1].epochMs,
  }));

  const byHour = Array.from({ length: 24 }, () => 0);
  const byWeekday = Array.from({ length: 7 }, () => 0);
  const grid = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => 0));
  observations.forEach((point) => {
    byHour[point.hour] += 1;
    byWeekday[point.weekday] += 1;
    grid[point.weekday][point.hour] += 1;
  });

  const gaps = observations.flatMap((point) => point.previousGapMs === null ? [] : [point.previousGapMs]);
  const countsByMonth = new Map<string, number>();
  ordered.forEach((point) => {
    const key = monthKey(point.year, point.month);
    countsByMonth.set(key, (countsByMonth.get(key) ?? 0) + 1);
  });
  const longPauseRanges = observations.slice(1).flatMap((point, index) => {
    const previous = observations[index];
    return point.epochMs - previous.epochMs > LONG_PAUSE_MS
      ? [{ start: previous.epochMs + LONG_PAUSE_MS, end: point.epochMs }]
      : [];
  });
  const months: PublishingMonth[] = [];
  if (ordered.length) {
    const first = ordered[0], last = ordered[ordered.length - 1];
    for (let year = first.year, month = first.month;
      year < last.year || (year === last.year && month <= last.month);
      month === 11 ? (year += 1, month = 0) : (month += 1)) {
      const start = zonedDayStart(year, month, 1, zone);
      const end = month === 11 ? zonedDayStart(year + 1, 0, 1, zone) : zonedDayStart(year, month + 1, 1, zone);
      months.push({
        key: monthKey(year, month),
        year,
        month,
        count: countsByMonth.get(monthKey(year, month)) ?? 0,
        hasLongPause: longPauseRanges.some((range) => range.start < end && range.end > start),
      });
    }
  }

  const peak = peakGridCell(grid);
  return {
    observations,
    byHour,
    byWeekday,
    grid,
    medianGapMs: median(gaps),
    maxGapMs: gaps.length ? gaps.reduce((largest, gap) => Math.max(largest, gap)) : null,
    peakHour: peak?.hour ?? null,
    peakWeekday: peak?.weekday ?? null,
    months,
  };
}

export function publishingWindow80(values: readonly number[]): PublishingWindow | null {
  if (values.length !== 24) return null;
  const total = values.reduce((sum, value) => sum + Math.max(0, value), 0);
  if (total <= 0) return null;
  const target = total * 0.8;
  let best: PublishingWindow | null = null;
  for (let start = 0; start < 24; start += 1) {
    let count = 0;
    for (let length = 1; length <= 24; length += 1) {
      count += Math.max(0, values[(start + length - 1) % 24]);
      if (count < target) continue;
      const candidate: PublishingWindow = {
        startHour: start,
        endHour: (start + length - 1) % 24,
        durationHours: length,
        count,
        share: count / total,
      };
      if (
        !best
        || candidate.durationHours < best.durationHours
        || (candidate.durationHours === best.durationHours && candidate.count > best.count)
      ) best = candidate;
      break;
    }
  }
  return best;
}

function circularMeanHour(values: readonly number[]): number | null {
  if (!values.length) return null;
  const vector = values.reduce((result, value) => {
    const angle = value / 24 * Math.PI * 2;
    return {
      x: result.x + Math.cos(angle),
      y: result.y + Math.sin(angle),
    };
  }, { x: 0, y: 0 });
  if (Math.abs(vector.x) < 1e-9 && Math.abs(vector.y) < 1e-9) return null;
  const angle = Math.atan2(vector.y, vector.x);
  return ((angle < 0 ? angle + Math.PI * 2 : angle) / (Math.PI * 2)) * 24;
}

export function weekdayEdgeYearSlotComparison(
  observations: readonly PublishingObservation[],
  weekday: number,
): EdgeYearSlotComparison | null {
  const selected = observations.filter((point) => point.weekday === weekday);
  if (!selected.length) return null;
  const years = selected.map((point) => point.year);
  // No argument spread: engines cap argument counts well below large catalogues.
  const fromYear = years.reduce((smallest, year) => Math.min(smallest, year));
  const toYear = years.reduce((largest, year) => Math.max(largest, year));
  if (fromYear === toYear) return null;
  const valuesForYear = (year: number) => selected
    .filter((point) => point.year === year)
    .map((point) => point.hour + point.minute / 60);
  const fromValues = valuesForYear(fromYear);
  const toValues = valuesForYear(toYear);
  if (
    fromValues.length < MIN_EDGE_YEAR_SLOT_OBSERVATIONS
    || toValues.length < MIN_EDGE_YEAR_SLOT_OBSERVATIONS
  ) return null;
  const fromMean = circularMeanHour(fromValues);
  const toMean = circularMeanHour(toValues);
  if (fromMean === null || toMean === null) return null;
  let hours = toMean - fromMean;
  if (hours > 12) hours -= 24;
  if (hours < -12) hours += 24;
  return {
    fromYear,
    toYear,
    hours,
    fromCount: fromValues.length,
    toCount: toValues.length,
  };
}

export function formatHour(hour: number): string {
  const normalized = ((Math.round(hour) % 24) + 24) % 24;
  return `${String(normalized).padStart(2, '0')}:00`;
}

export function formatPublishingGap(gapMs: number | null): string {
  if (gapMs === null) return 'Not enough intervals';
  const hours = gapMs / HOUR_MS;
  if (hours < 48) {
    return `${new Intl.NumberFormat('en', { maximumFractionDigits: 1 }).format(hours)} hours`;
  }
  return `${new Intl.NumberFormat('en', { maximumFractionDigits: 1 }).format(hours / 24)} days`;
}
