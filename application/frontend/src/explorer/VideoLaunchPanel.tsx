import { useSourceExpiry } from '../sourceLifetimeReact';
import { SOURCE_EXPIRED_MESSAGE, sourceExpired } from '../sourceLifetime.ts';
import { useEffect, useRef, useState } from 'react';
import type { VideoLaunchPayload } from '../types';
import { ExplorerApiError, isAbortError, loadVideoLaunch } from './api';
import { formatCount } from './format';
import { formatExactLocal } from './localTime';
import { LAUNCH_HORIZONS, launchAgeLabel, launchClassificationLabel, launchMultiplierLabel, launchStatusLabel } from './videoLaunch';
import type { StaticInspectorContext } from './VideoStaticDetailsPanel';
import './video-launch.css';

type State = { key: string; kind: 'loading' | 'unavailable' | 'error'; message?: string }
  | { key: string; kind: 'ready'; payload: VideoLaunchPayload; refreshError?: string };
const exactTime = (value: string) => formatExactLocal(value);

/** A separately gated, explicit action; opening an Inspector alone never reads channel snapshot history. */
export function VideoLaunchPanel({ channelId, videoId, browseSession, active }: StaticInspectorContext & { videoId: string }) {
  const key = JSON.stringify([channelId, videoId, browseSession]);
  const live = useRef({ key, active });
  live.current = { key, active };
  const [request, setRequest] = useState<{ key: string; attempt: number } | null>(null);
  const [state, setState] = useState<State>({ key, kind: 'loading' });
  const requested = request?.key === key;
  const [, repaint] = useState(0), reading = useRef(false);
  useSourceExpiry(state, () => repaint(value => value + 1), () => { if (active && requested && !reading.current) setRequest(previous => ({ key, attempt: (previous?.attempt ?? 0) + 1 })); });
  useEffect(() => {
    if (!active || !requested) return;
    reading.current = true;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && live.current.active && live.current.key === key;
    setState(previous => previous.key === key && previous.kind === 'ready' ? previous : { key, kind: 'loading' });
    loadVideoLaunch(channelId, videoId, browseSession, controller.signal).then((payload) => {
      if (!current()) return;
      setState({ key, kind: 'ready', payload });
    }).catch((error) => {
      if (!current() || isAbortError(error)) return;
      const unavailable = error instanceof ExplorerApiError && [403, 404, 410].includes(error.status);
      setState(previous => previous.key === key && previous.kind === 'ready' ? { ...previous, refreshError: SOURCE_EXPIRED_MESSAGE } : { key, kind: unavailable ? 'unavailable' : 'error', message: unavailable
        ? 'Launch performance is unavailable for this catalogue session.'
        : 'Launch performance could not load. The existing video details are kept.' });
    }).finally(() => { if (current()) reading.current = false; });
    return () => { controller.abort(); reading.current = false; };
  }, [key, channelId, videoId, browseSession, active, requested, request]);
  const payload = active && requested && state.key === key && state.kind === 'ready' ? state.payload : null;
  const start = () => setRequest((previous) => ({ key, attempt: (previous?.attempt ?? 0) + 1 }));
  return <section className="explorer-v2-video-observation explorer-v2-video-launch" aria-label="Launch performance">
    <header><h3>Launch performance</h3><span>24H · 7D · 28D</span></header>
    {!payload ? <>
      <p role="status">{!active ? 'The catalogue changed. Reopen this video in the current results for launch performance.'
        : !requested ? 'Compare real near-target snapshots with other long-form videos on this channel.'
          : state.key !== key || state.kind === 'loading' ? 'Comparing launch observations…'
            : state.kind === 'ready' ? 'This catalogue session expired.' : state.message}</p>
      {active && !requested && <button type="button" className="explorer-secondary-button" onClick={start}>Compare launch</button>}
      {active && requested && state.key === key && state.kind === 'error'
        && <button type="button" className="explorer-secondary-button" onClick={start}>Retry launch</button>}
    </> : <>
      {state.kind === 'ready' && state.refreshError && <p role="status">{state.refreshError} <button type="button" onClick={start}>Reload</button></p>}
      {payload.horizons.map((horizon, index) => {
        if (sourceExpired(horizon)) return null;
        const spec = LAUNCH_HORIZONS[index], target = horizon.target, comparison = target.comparison;
        const measured = target.status === 'ready', assessed = comparison.status === 'preliminary' || comparison.status === 'ready';
        const ratio = launchMultiplierLabel(comparison, target.views);
        return <article className="explorer-v2-launch-horizon" key={horizon.key} aria-label={spec.label}>
          <h4>{spec.label}</h4>
          {measured ? <>
            <p className="explorer-v2-launch-result"><strong>{formatCount(target.views, { compact: false })} views</strong>
              {assessed && <span>{launchClassificationLabel(comparison.classification)}</span>}</p>
            {ratio && <p className="explorer-v2-launch-ratio">{ratio}</p>}
            <p className="explorer-v2-launch-caption">Observed at age {launchAgeLabel(target.actual_age_seconds)}</p>
            {assessed ? <p className="explorer-v2-launch-caption">{comparison.reference_count} reference videos{comparison.status === 'preliminary' ? ' · Preliminary' : ''}</p>
              : <p className="explorer-v2-launch-caption">At least 4 measured videos, including this one, are needed. No assessment yet.</p>}
          </> : <p className="explorer-v2-launch-caption">{launchStatusLabel(horizon)}</p>}
          <p className="explorer-v2-launch-caption">
            {horizon.eligible_count} measured videos{horizon.quality === 'preliminary' ? ' · Preliminary' : ''}
            {horizon.eligible_count >= 4 ? ` · ${horizon.counts.below_typical} below · ${horizon.counts.typical} typical · ${horizon.counts.above_typical} above` : ' · Not assessed'}
          </p>
          <details className="explorer-v2-launch-details"><summary>Measurement &amp; reference</summary>
            <p>{spec.evidence}. Latest real snapshot at or before the target; no interpolation.</p>
            <dl className="explorer-v2-video-drawer-details">
              {target.target_at && <div><dt>Target time</dt><dd>{exactTime(target.target_at)}</dd></div>}
              {target.observed_at && <div><dt>Observed at</dt><dd>{exactTime(target.observed_at)}</dd></div>}
              {assessed && <>
                <div><dt>Reference median</dt><dd>{formatCount(comparison.median, { compact: false })}</dd></div>
                <div><dt>P25–P75</dt><dd>{formatCount(comparison.p25, { compact: false })}–{formatCount(comparison.p75, { compact: false })}</dd></div>
              </>}
            </dl>
            <p>This video is excluded from its reference. Group counts assess each eligible video against the others, separately for each horizon.</p>
          </details>
        </article>;
      })}
      <details className="explorer-v2-launch-details"><summary>Data window &amp; limits</summary>
        <p>{exactTime(payload.window.from_at)} → {exactTime(payload.window.to_at)}</p>
        <p>Only real snapshots from this 28-day window are used. Missing early history is not reconstructed from current views. Near-target observations are not exact target-time totals.</p>
        <p>All-time long-form publications; catalogue filters do not change the reference. No duration or age weighting. Below 4 measured videos: no assessment. 4–11: Preliminary.</p>
        {!sourceExpired(payload.coverage) && <p>{payload.coverage.eligible_video_count} eligible videos · {payload.coverage.excluded_video_count} excluded by data eligibility.</p>}
      </details>
    </>}
    <small>Leti Cook calculation · Real snapshots from last 28 days</small>
  </section>;
}
