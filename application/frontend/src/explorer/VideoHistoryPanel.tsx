import { useSourceExpiry } from '../sourceLifetimeReact';
import { SOURCE_EXPIRED_MESSAGE } from '../sourceLifetime.ts';
import { useEffect, useRef, useState } from 'react';
import type { VideoHistoryPayload } from '../types';
import { ExplorerApiError, isAbortError, loadVideoHistory } from './api';
import { formatCount } from './format';
import { utcTimestampMicroseconds } from './contracts';
import { formatExactLocal } from './localTime';
import { HISTORY_METRICS, historyExactCount, historyExactTime, historyIntervalLabel, historyIntervalRate, videoHistoryGeometry, videoHistorySeries, type HistoryMetric, type HistoryMode } from './videoHistory';
import './video-history.css';

export type HistoryInspectorContext = { channelId: string; browseSession: string; active: boolean; checkedAt: string | null };
type State = { key: string; kind: 'loading' | 'unavailable' | 'error'; message?: string }
  | { key: string; kind: 'ready'; payload: VideoHistoryPayload; refreshError?: string };

/** Mounted only for an explicitly gated Inspector opened from a frozen Growth result. */
export function VideoHistoryPanel({ channelId, videoId, browseSession, active, checkedAt }: HistoryInspectorContext & { videoId: string }) {
  const key = JSON.stringify([channelId, videoId, browseSession, checkedAt]);
  const live = useRef({ key, active });
  live.current = { key, active };
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<State>({ key, kind: 'loading' });
  const [, repaint] = useState(0), reading = useRef(false);
  useSourceExpiry(state, () => repaint(value => value + 1), () => { if (active && !reading.current) setRetry(value => value + 1); });
  const [mode, setMode] = useState<HistoryMode>('cumulative');
  const [metric, setMetric] = useState<HistoryMetric>('views');
  const [selected, setSelected] = useState<number | null>(null);
  useEffect(() => {
    setMode('cumulative'); setMetric('views'); setSelected(null);
  }, [key]);
  useEffect(() => {
    if (!active) return;
    reading.current = true;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && live.current.active && live.current.key === key;
    setState(previous => previous.key === key && previous.kind === 'ready' ? previous : { key, kind: 'loading' });
    loadVideoHistory(channelId, videoId, browseSession, controller.signal).then((payload) => {
      if (!current()) return;
      if (HISTORY_METRICS.some((name) => {
        const series = videoHistorySeries(payload, name);
        return series?.status === 'ready' && utcTimestampMicroseconds(series.window.to_at) !== utcTimestampMicroseconds(checkedAt);
      })) {
        setState({ key, kind: 'error', message: 'History does not match the opened video observation.' }); return;
      }
      setState({ key, kind: 'ready', payload });
    }).catch((error) => {
      if (!current() || isAbortError(error)) return;
      const unavailable = error instanceof ExplorerApiError && [403, 404, 410].includes(error.status);
      setState(previous => previous.key === key && previous.kind === 'ready' ? { ...previous, refreshError: SOURCE_EXPIRED_MESSAGE }
        : { key, kind: unavailable ? 'unavailable' : 'error', message: unavailable
          ? 'History is unavailable for this catalogue session.' : 'History could not load.' });
    }).finally(() => { if (current()) reading.current = false; });
    return () => { controller.abort(); reading.current = false; };
  }, [key, channelId, videoId, browseSession, checkedAt, active, retry]);
  const payload = active && state.key === key && state.kind === 'ready' ? state.payload : null;
  const heading = <header><h3>Recent {metric}</h3><span title="Strictly the 28 days ending at the catalogue observation; this can differ from the comparison interval above.">28-day observation window</span></header>;
  if (!payload) return <section className="explorer-v2-video-observation explorer-v2-video-history" aria-label="Recent video history">
    {heading}
    <p role="status">{!active ? 'The catalogue changed. Reopen this video in the current results for history.'
      : state.key !== key || state.kind === 'loading' ? 'Loading stored observations…'
        : state.kind === 'ready' ? 'This catalogue session expired.' : state.message}</p>
    {active && state.key === key && state.kind === 'error' && <button type="button" className="explorer-secondary-button"
      onClick={() => setRetry((value) => value + 1)}>Retry history</button>}
  </section>;
  const series = videoHistorySeries(payload, metric);
  const metricSelector = <div className="explorer-v2-history-modes explorer-v2-history-metrics" role="group" aria-label="History metric">
    {HISTORY_METRICS.map((name) => <button key={name} type="button" aria-pressed={metric === name}
      disabled={name !== 'views' && !payload.interaction_metrics} onClick={() => {
        setMetric(name); setSelected(null);
        if (!videoHistorySeries(payload, name)?.intervals.length) setMode('cumulative');
      }}>{name[0].toUpperCase() + name.slice(1)}</button>)}
  </div>;
  if (!series || series.status === 'unavailable') return <section className="explorer-v2-video-observation explorer-v2-video-history" aria-label="Recent video history">
    {heading}{metricSelector}<p role="status">{!series ? `Stored ${metric} history is not available from this server.`
      : series.reason === 'conflicting_simultaneous_observations' ? `Stored ${metric} observations conflict at the same timestamp.`
        : `Stored ${metric} history cannot be matched to this video’s catalogue observation.`}</p>
  </section>;
  const geometry = videoHistoryGeometry(payload, mode, metric);
  const count = mode === 'cumulative' ? series.points.length : series.intervals.length;
  const index = Math.max(0, Math.min(selected ?? count - 1, count - 1));
  const point = mode === 'cumulative' ? series.points[index] : null;
  const interval = mode === 'interval' ? series.intervals[index] : null;
  const valueText = point ? `${historyExactCount(point.value)}${point.value === null ? '' : ` total ${metric}`}`
    : interval ? `${historyExactCount(interval.delta, true)}${interval.delta === null ? '' : ` ${metric} gained`}` : 'No adjacent observations';
  const timeText = point ? historyExactTime(point.recorded_at) : interval ? `${historyExactTime(interval.from_at)} → ${historyExactTime(interval.to_at)}` : '';
  const setModeAndReset = (next: HistoryMode) => { setMode(next); setSelected(null); };
  return <section className="explorer-v2-video-observation explorer-v2-video-history" aria-label="Recent video history">
    {heading}{metricSelector}
    {state.kind === 'ready' && state.refreshError && <p role="status">{state.refreshError} <button type="button" onClick={() => setRetry(value => value + 1)}>Reload</button></p>}
    <div className="explorer-v2-history-modes" role="group" aria-label="History display">
      <button type="button" aria-pressed={mode === 'cumulative'} onClick={() => setModeAndReset('cumulative')}>Cumulative</button>
      <button type="button" aria-pressed={mode === 'interval'} disabled={!series.intervals.length} onClick={() => setModeAndReset('interval')}>Interval gain</button>
    </div>
    <p className="explorer-v2-history-caption">{mode === 'cumulative' ? `Stored total ${metric} at real observation times.` : 'Change across each actual interval — not daily values.'}</p>
    {geometry ? <svg className="explorer-v2-history-chart" viewBox="0 0 400 198" role="img" tabIndex={0}
      aria-label={`${mode === 'cumulative' ? `Cumulative ${metric}` : `Interval ${metric} gained`}. ${valueText}. ${timeText}. Use Left and Right arrows to inspect observations.`}
      onKeyDown={(event) => {
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? count - 1
          : event.key === 'ArrowLeft' || event.key === 'ArrowDown' ? Math.max(0, index - 1)
            : event.key === 'ArrowRight' || event.key === 'ArrowUp' ? Math.min(count - 1, index + 1) : null;
        if (next !== null) { event.preventDefault(); setSelected(next); }
      }}>
      <title>{mode === 'cumulative' ? `Real stored ${metric} observations` : 'Observed change across real adjacent intervals'}</title>
      <line className="explorer-v2-history-guide" x1="54" x2="384" y1="20" y2="20" />
      <line className="explorer-v2-history-guide" x1="54" x2="384" y1="160" y2="160" />
      <text x="48" y="24" textAnchor="end">{formatCount(geometry.high)}</text>
      <text x="48" y="164" textAnchor="end">{formatCount(geometry.low)}</text>
      {geometry.zeroY !== null && <line className="explorer-v2-history-zero" x1="54" x2="384" y1={geometry.zeroY} y2={geometry.zeroY} />}
      {geometry.points.map((item) => <g key={item.index} className={`${item.index === index ? 'is-selected' : ''} ${item.value?.startsWith('-') ? 'is-correction' : ''}`}
        onMouseEnter={() => setSelected(item.index)} onClick={() => setSelected(item.index)}>
        <title>{`${historyExactTime(item.from)}${item.from === item.to ? '' : ` → ${historyExactTime(item.to)}`}: ${historyExactCount(item.value, mode === 'interval')}`}</title>
        {item.y === null ? <path className="explorer-v2-history-missing" d={`M${item.x - 3},174 l6,6 m-6,0 l6,-6`} />
          : <>{mode === 'interval' && <line className="explorer-v2-history-interval" x1={item.fromX} x2={item.x} y1={item.y} y2={item.y} />}
            <circle className="explorer-v2-history-point" cx={item.x} cy={item.y} r={item.index === index ? 4.5 : 3} />
            <circle className="explorer-v2-history-hit" cx={item.x} cy={item.y} r="9" /></>}
      </g>)}
      <text x="54" y="195">{formatExactLocal(series.window.from_at).slice(0, 10)}</text>
      <text x="384" y="195" textAnchor="end">{formatExactLocal(series.window.to_at).slice(0, 10)}</text>
    </svg> : <p role="status">No measured values for these intervals.</p>}
    <div className="explorer-v2-history-point-details" aria-live="polite" aria-atomic="true">
      <strong>{valueText}</strong><time>{timeText}</time>
      {interval && <span>Actual interval: {historyIntervalLabel(interval.actual_interval_seconds)}</span>}
      {interval?.delta?.startsWith('-') && <span>Possible counter correction.</span>}
    </div>
    <div className="explorer-v2-history-navigation" role="group" aria-label="Inspect history observations">
      <button type="button" className="explorer-secondary-button" aria-label="Previous observation" aria-disabled={index === 0} onClick={() => { if (index > 0) setSelected(index - 1); }}>←</button>
      <span>{count ? index + 1 : 0} / {count} {mode === 'cumulative' ? 'observations' : 'intervals'}</span>
      <button type="button" className="explorer-secondary-button" aria-label="Next observation" aria-disabled={index >= count - 1} onClick={() => { if (index < count - 1) setSelected(index + 1); }}>→</button>
    </div>
    <p className="explorer-v2-history-latest"><span>Latest interval rate</span><strong>{historyIntervalRate(series.latest_interval, metric)}</strong>
      {series.latest_interval && <small title={`${historyExactTime(series.latest_interval.from_at)} → ${historyExactTime(series.latest_interval.to_at)}`}>
        {historyExactCount(series.latest_interval.delta, true)} {metric} over {historyIntervalLabel(series.latest_interval.actual_interval_seconds)}
        {series.latest_interval.delta?.startsWith('-') ? ' · Possible counter correction' : ''}
      </small>}
    </p>
    <small className="explorer-v2-history-caption">{series.points.length} available observations{series.points.filter(point => point.value === null).length ? ` · missing ${metric} marked ×` : ''}.
      {' No interpolation. This window ends at the catalogue observation and may differ from the comparison above. Interval changes and rates: Leti Cook calculations.'}</small>
  </section>;
}
