import { sourceExpired } from '../sourceLifetime';
import { useEffect, useId, useMemo, useState, type KeyboardEvent } from 'react';
import type {
  ChannelBaselinePublicationCalendar,
  ChannelBaselinePublicationPoint,
} from '../types';
import {
  buildPublicationCalendarView,
  buildPublishingRhythm,
  calendarYearOptions,
  formatHour,
  publicationCalendarHeatLevel,
  publishingRhythmSelectionKey,
} from './publishingRhythm';
import { videoNoun } from './format';
import { useTimeBasis } from './timeBasis';
import { timeZoneLabel } from './timeZones';

const DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const DAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const SVG_SIZE = 300;
const CENTER = SVG_SIZE / 2;

function rangeDate(zone: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat('en', { timeZone: zone, year: 'numeric', month: 'short', day: 'numeric' });
}

const utcCalendarDate = new Intl.DateTimeFormat('en', {
  timeZone: 'UTC',
  year: 'numeric',
  month: 'short',
  day: 'numeric',
});

function polar(radius: number, degrees: number): [number, number] {
  const angle = (degrees - 90) * Math.PI / 180;
  return [CENTER + radius * Math.cos(angle), CENTER + radius * Math.sin(angle)];
}

function sectorPath(inner: number, outer: number, start: number, end: number): string {
  const outerStart = polar(outer, start);
  const outerEnd = polar(outer, end);
  const innerEnd = polar(inner, end);
  const innerStart = polar(inner, start);
  return [
    `M ${outerStart[0]} ${outerStart[1]}`,
    `A ${outer} ${outer} 0 0 1 ${outerEnd[0]} ${outerEnd[1]}`,
    `L ${innerEnd[0]} ${innerEnd[1]}`,
    `A ${inner} ${inner} 0 0 0 ${innerStart[0]} ${innerStart[1]}`,
    'Z',
  ].join(' ');
}

type RhythmSelection = {
  type: 'hour' | 'weekday';
  index: number;
} | null;

function uploadLabel(value: number): string {
  return `${value} ${value === 1 ? 'upload' : 'uploads'}`;
}

function shareLabel(value: number, total: number): string {
  if (total <= 0) return '0%';
  return `${(value * 100 / total).toFixed(1)}%`;
}

function radialFillOuter(inner: number, outer: number, value: number, maximum: number): number {
  if (value <= 0 || maximum <= 0) return inner;
  return inner + 4 + value / maximum * (outer - inner - 4);
}

function maximumIndex(values: readonly number[]): number | null {
  const maximum = Math.max(0, ...values);
  return maximum <= 0 ? null : values.findIndex((value) => value === maximum);
}

function activateSvgSegment(
  event: KeyboardEvent<SVGGElement>,
  activate: () => void,
) {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  activate();
}

function RingDial({
  byHour,
  byWeekday,
  total,
  selection,
  onSelect,
  descriptionId,
  zoneLabel,
}: {
  byHour: number[];
  byWeekday: number[];
  total: number;
  selection: RhythmSelection;
  onSelect: (selection: Exclude<RhythmSelection, null>) => void;
  descriptionId: string;
  zoneLabel: string;
}) {
  const hourMaximum = Math.max(0, ...byHour);
  const weekdayMaximum = Math.max(0, ...byWeekday);
  const peakHour = maximumIndex(byHour);
  const peakCount = peakHour === null ? 0 : (byHour[peakHour] ?? 0);
  const selectedValue = selection?.type === 'hour'
    ? (byHour[selection.index] ?? 0)
    : selection?.type === 'weekday'
      ? (byWeekday[selection.index] ?? 0)
      : peakCount;
  const centerValue = selection?.type === 'hour'
    ? formatHour(selection.index)
    : selection?.type === 'weekday'
      ? DAY_SHORT[selection.index]
      : peakHour === null ? '—' : formatHour(peakHour);
  const centerCaption = selection?.type === 'hour'
    ? selection.index === peakHour ? `Hour, ${zoneLabel} · peak` : `Hour, ${zoneLabel}`
    : selection?.type === 'weekday'
      ? `Weekday · ${zoneLabel}`
      : `Peak hour, ${zoneLabel}`;
  const centerSub = `${uploadLabel(selectedValue)} · ${shareLabel(selectedValue, total)}`;

  return (
    <svg
      className={`explorer-rhythm-dial explorer-rhythm-two-ring${selection ? ' has-selection' : ''}`}
      viewBox={`0 0 ${SVG_SIZE} ${SVG_SIZE}`}
      role="group"
      aria-label={`Publication frequency by hour (${zoneLabel}) and weekday`}
      aria-describedby={descriptionId}
    >
      <g className="explorer-rhythm-ticks" aria-hidden="true">
        {[0, 6, 12, 18].map((hour) => {
          const [x, y] = polar(141, hour * 15);
          return <text key={hour} x={x} y={y + 3} textAnchor="middle">{String(hour).padStart(2, '0')}</text>;
        })}
      </g>

      <g className="explorer-rhythm-overall-ring explorer-rhythm-hour-ring">
        {byHour.map((value, hour) => {
          const selected = selection?.type === 'hour' && selection.index === hour;
          const peak = hour === peakHour;
          const fraction = hourMaximum > 0 ? value / hourMaximum : 0;
          const choose = () => onSelect({ type: 'hour', index: hour });
          const label = `${formatHour(hour)} ${zoneLabel}, ${uploadLabel(value)}, ${shareLabel(value, total)} of stored publications${peak ? ', peak hour' : ''}`;
          return (
            <g
              key={hour}
              className={`explorer-rhythm-segment explorer-rhythm-hour-segment${selected ? ' is-selected' : ''}${value === 0 ? ' is-zero' : ''}${peak ? ' is-peak' : ''}`}
              role="button"
              tabIndex={0}
              aria-label={label}
              aria-pressed={selected}
              onClick={choose}
              onKeyDown={(event) => activateSvgSegment(event, choose)}
            >
              <title>{label}</title>
              <path
                className="explorer-rhythm-segment-track"
                d={sectorPath(74, 124, hour * 15 + 1.6, hour * 15 + 13.4)}
                fill="rgba(125, 160, 190, 0.06)"
                style={{
                  stroke: selected ? '#edf5fb' : '#090d12',
                  strokeWidth: selected ? 1.2 : 0.8,
                }}
              />
              {value > 0 && (
                <path
                  className="explorer-rhythm-segment-fill"
                  d={sectorPath(74, radialFillOuter(74, 124, value, hourMaximum), hour * 15 + 1.6, hour * 15 + 13.4)}
                  fill={selected
                    ? '#66e3b4'
                    : peak
                      ? '#66e3b4'
                      : `rgba(81, 185, 255, ${(0.16 + 0.6 * fraction).toFixed(2)})`}
                  style={{
                    stroke: selected ? '#edf5fb' : '#090d12',
                    strokeWidth: selected ? 1.2 : 0.8,
                  }}
                  pointerEvents="none"
                />
              )}
            </g>
          );
        })}
      </g>

      <g className="explorer-rhythm-weekday-ring">
        {byWeekday.map((value, day) => {
          const selected = selection?.type === 'weekday' && selection.index === day;
          const fraction = weekdayMaximum > 0 ? value / weekdayMaximum : 0;
          const start = day * (360 / 7) + 2;
          const end = (day + 1) * (360 / 7) - 2;
          const choose = () => onSelect({ type: 'weekday', index: day });
          const label = `${DAY_NAMES[day]}, ${uploadLabel(value)}, ${shareLabel(value, total)} of stored publications`;
          return (
            <g
              key={DAY_NAMES[day]}
              className={`explorer-rhythm-segment explorer-rhythm-weekday-segment${selected ? ' is-selected' : ''}${value === 0 ? ' is-zero' : ''}`}
              role="button"
              tabIndex={0}
              aria-label={label}
              aria-pressed={selected}
              onClick={choose}
              onKeyDown={(event) => activateSvgSegment(event, choose)}
            >
              <title>{label}</title>
              <path
                className="explorer-rhythm-segment-track"
                d={sectorPath(50, 68, start, end)}
                fill="rgba(125, 160, 190, 0.05)"
                style={{
                  stroke: selected ? '#edf5fb' : '#090d12',
                  strokeWidth: selected ? 1.2 : 0.8,
                }}
              />
              {value > 0 && (
                <path
                  className="explorer-rhythm-segment-fill"
                  d={sectorPath(50, radialFillOuter(50, 68, value, weekdayMaximum), start, end)}
                  fill={selected
                    ? '#66e3b4'
                    : `rgba(167, 139, 250, ${(0.18 + 0.68 * fraction).toFixed(2)})`}
                  style={{
                    stroke: selected ? '#edf5fb' : '#090d12',
                    strokeWidth: selected ? 1.2 : 0.8,
                  }}
                  pointerEvents="none"
                />
              )}
            </g>
          );
        })}
      </g>

      <g className="explorer-rhythm-hub" aria-hidden="true">
        <text className="is-value" x={CENTER} y={148} textAnchor="middle">{centerValue}</text>
        <text className="is-caption" x={CENTER} y={163} textAnchor="middle">{centerCaption}</text>
        <text className="is-sub" x={CENTER} y={178} textAnchor="middle">{centerSub}</text>
      </g>
    </svg>
  );
}

function PublicationCalendarHeatmap({
  calendar,
}: {
  calendar?: ChannelBaselinePublicationCalendar;
}) {
  const options = useMemo(() => calendarYearOptions(calendar), [calendar]);
  const [selectedYear, setSelectedYear] = useState(
    () => calendar?.default_year ?? new Date().getUTCFullYear(),
  );
  useEffect(() => {
    setSelectedYear((current) => (
      options.includes(current) ? current : calendar?.default_year ?? options[0]
    ));
  }, [calendar, options]);
  const view = useMemo(
    () => buildPublicationCalendarView(calendar, selectedYear),
    [calendar, selectedYear],
  );
  const positiveDays = view.cells.filter((cell) => cell.date && cell.count > 0);

  return (
    <section className="explorer-rhythm-calendar explorer-content-subsection" aria-labelledby="publishing-calendar-title">
      <header>
        <div>
          <h3 id="publishing-calendar-title">Publication calendar</h3>
          <p>Daily uploads in the selected calendar year ({timeZoneLabel(calendar?.timezone ?? 'UTC')}). This year selector is independent from the Overview period.</p>
        </div>
        <label className="explorer-rhythm-year-control">
          <span>Year</span>
          <select
            aria-label="Publication calendar year"
            value={selectedYear}
            onChange={(event) => setSelectedYear(Number(event.target.value))}
          >
            {options.map((year) => <option value={year} key={year}>{year}</option>)}
          </select>
        </label>
      </header>

      <div className="explorer-rhythm-calendar-meta">
        <strong>{view.total} {view.total === 1 ? 'upload' : 'uploads'}</strong>
        <span>{selectedYear} · {timeZoneLabel(calendar?.timezone ?? 'UTC')}</span>
      </div>

      <div
        className="explorer-rhythm-calendar-scroll"
        role="region"
        tabIndex={0}
        aria-label={`Daily publication calendar for ${selectedYear}; scroll horizontally if needed`}
      >
        <div className="explorer-rhythm-calendar-layout" aria-hidden="true">
          <div className="explorer-rhythm-calendar-weekdays">
            <span>Mon</span><span /><span>Wed</span><span /><span>Fri</span><span /><span />
          </div>
          <div className="explorer-rhythm-calendar-chart">
            <div
              className="explorer-rhythm-calendar-months"
              style={{ gridTemplateColumns: `repeat(${view.weekCount}, minmax(0, 1fr))` }}
            >
              {view.months.map((month) => (
                <span key={month.key} style={{ gridColumn: `${month.week + 1} / span 3` }}>{month.label}</span>
              ))}
            </div>
            <div
              className="explorer-rhythm-calendar-cells"
              style={{
                gridTemplateColumns: `repeat(${view.weekCount}, minmax(0, 1fr))`,
                gridTemplateRows: 'repeat(7, 8px)',
              }}
            >
              {view.cells.map((cell, index) => (
                <span
                  key={cell.date ?? `outside-${index}`}
                  className={cell.inYear
                    ? `is-level-${publicationCalendarHeatLevel(cell.count, view.maximum)}`
                    : 'is-outside'}
                  title={cell.date
                    ? `${utcCalendarDate.format(new Date(`${cell.date}T00:00:00Z`))}: ${cell.count} ${cell.count === 1 ? 'upload' : 'uploads'}`
                    : undefined}
                />
              ))}
            </div>
          </div>
        </div>
      </div>

      {positiveDays.length > 0 ? (
        <ol className="explorer-sr-only">
          {positiveDays.map((cell) => (
            <li key={cell.date}>{cell.date}: {cell.count} {cell.count === 1 ? 'upload' : 'uploads'}</li>
          ))}
        </ol>
      ) : (
        <p className="explorer-rhythm-calendar-empty">No stored publication dates in {selectedYear}.</p>
      )}
      <div className="explorer-rhythm-calendar-legend" aria-hidden="true">
        <span>Less</span>
        {[0, 1, 2, 3, 4, 5].map((level) => <i className={`is-level-${level}`} key={level} />)}
        <span>More</span>
      </div>
    </section>
  );
}

export function PublishingRhythm({
  publicationPoints,
  publicationCalendar,
  selectionScopeKey,
}: {
  publicationPoints?: ChannelBaselinePublicationPoint[];
  publicationCalendar?: ChannelBaselinePublicationCalendar;
  selectionScopeKey?: string;
}) {
  const { zone } = useTimeBasis();
  const zoneLabel = timeZoneLabel(zone);
  const data = useMemo(
    () => buildPublishingRhythm(publicationPoints ?? [], zone),
    [publicationPoints, zone],
  );
  const [selection, setSelection] = useState<RhythmSelection>(null);
  const selectionKey = useMemo(
    () => publishingRhythmSelectionKey(selectionScopeKey, publicationPoints),
    [publicationPoints, selectionScopeKey],
  );
  const rhythmDescriptionId = `publishing-rhythm-description-${useId().replace(/:/g, '')}`;

  useEffect(() => {
    setSelection(null);
  }, [selectionKey]);

  // Rhythm percentages depend on the whole observation sample. Do not turn
  // removal of an expired publication into a newly calculated partial sample.
  if (publicationPoints?.some(point => sourceExpired(point)) || sourceExpired(publicationCalendar)) {
    return <section className="explorer-publishing-rhythm explorer-content-section"><h2>Publishing rhythm</h2><p role="status">Refreshing current data…</p></section>;
  }

  if (!data.observations.length) {
    return (
      <section className="explorer-publishing-rhythm explorer-content-section is-level-1">
        <header className="explorer-rhythm-heading explorer-content-heading">
          <h2 className="explorer-eyebrow">Publishing rhythm · {zoneLabel}</h2>
          <strong>No publications in the active Overview period</strong>
        </header>
        <div className="explorer-baseline-empty is-compact" role="status">
          <span className="explorer-rhythm-empty-mark" aria-hidden="true">—</span>
          <span>{publicationPoints === undefined
            ? 'Exact period publication points are unavailable on this server version.'
            : 'No valid publication timestamps are available for this period.'}</span>
        </div>
        <PublicationCalendarHeatmap calendar={publicationCalendar} />
      </section>
    );
  }

  const total = data.observations.length;
  const peakHour = maximumIndex(data.byHour);
  const first = data.observations[0];
  const last = data.observations[data.observations.length - 1];
  const selectedCount = selection?.type === 'hour'
    ? (data.byHour[selection.index] ?? 0)
    : selection?.type === 'weekday'
      ? (data.byWeekday[selection.index] ?? 0)
      : peakHour === null ? 0 : (data.byHour[peakHour] ?? 0);
  const selectionStatus = selection?.type === 'hour'
    ? `${formatHour(selection.index)} ${zoneLabel}: ${uploadLabel(selectedCount)}, ${shareLabel(selectedCount, total)} of stored publications.`
    : selection?.type === 'weekday'
      ? `${DAY_NAMES[selection.index]}: ${uploadLabel(selectedCount)}, ${shareLabel(selectedCount, total)} of stored publications.`
      : peakHour === null
        ? 'No peak publication hour is available.'
        : `Peak hour ${formatHour(peakHour)} ${zoneLabel}: ${uploadLabel(selectedCount)}, ${shareLabel(selectedCount, total)} of stored publications.`;

  const handleSelection = (next: Exclude<RhythmSelection, null>) => {
    setSelection((current) => (
      current?.type === next.type && current.index === next.index ? null : next
    ));
  };

  const handleEscape = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape' || selection === null) return;
    event.stopPropagation();
    setSelection(null);
  };

  return (
    <section
      className={`explorer-publishing-rhythm explorer-content-section${selection ? ' has-selection' : ''}`}
      aria-labelledby="publishing-rhythm-title"
      onKeyDown={handleEscape}
    >
      <header className="explorer-rhythm-heading explorer-content-heading">
        <h2 className="explorer-eyebrow" id="publishing-rhythm-title">Publishing rhythm · {zoneLabel}</h2>
        <strong>{total} {videoNoun(total)} · {rangeDate(zone).format(first.epochMs)} – {rangeDate(zone).format(last.epochMs)}</strong>
      </header>

      <div className="explorer-rhythm-stage">
        <RingDial
          byHour={data.byHour}
          byWeekday={data.byWeekday}
          total={total}
          selection={selection}
          onSelect={handleSelection}
          descriptionId={rhythmDescriptionId}
          zoneLabel={zoneLabel}
        />
      </div>

      <p id={rhythmDescriptionId} className="explorer-sr-only">
        Outer ring shows stored publication counts by hour ({zoneLabel}). Inner ring shows counts by weekday. Activate a segment for details; activate it again or press Escape to reset. Complete values follow in an accessible table.
      </p>
      <p className="explorer-sr-only" role="status" aria-live="polite">{selectionStatus}</p>
      <p className="explorer-rhythm-ring-hint">
        Outer ring — hours ({zoneLabel}) · inner ring — weekdays. Select a segment for details; select it again to reset.
      </p>
      <div className="explorer-sr-only">
        <table aria-describedby={rhythmDescriptionId}>
          <caption>Publication counts by weekday and hour ({zoneLabel})</caption>
          <thead>
            <tr>
              <th scope="col">Weekday</th>
              {data.byHour.map((_, hour) => (
                <th scope="col" key={hour}>{formatHour(hour)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">All weekdays</th>
              {data.byHour.map((count, hour) => <td key={hour}>{count}</td>)}
            </tr>
            {data.grid.map((hours, day) => (
              <tr key={DAY_NAMES[day]}>
                <th scope="row">{DAY_NAMES[day]}</th>
                {hours.map((count, hour) => <td key={hour}>{count}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <PublicationCalendarHeatmap calendar={publicationCalendar} />
    </section>
  );
}
