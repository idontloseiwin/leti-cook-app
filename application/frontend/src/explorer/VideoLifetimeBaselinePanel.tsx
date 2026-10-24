import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired } from '../sourceLifetime.ts';
import { useEffect, useRef, useState } from 'react';
import type { VideoLifetimeBaselinePayload } from '../types';
import { ExplorerApiError, isAbortError, loadVideoLifetimeBaseline } from './api';
import { formatCount, videoNoun } from './format';
import type { StaticInspectorContext } from './VideoStaticDetailsPanel';

type State = { key: string; kind: 'loading' | 'unavailable' | 'error'; message?: string }
  | { key: string; kind: 'ready'; payload: VideoLifetimeBaselinePayload };

/** Independent Baseline-gated request; opening context controls admission, never the reference filters. */
export function VideoLifetimeBaselinePanel({ channelId, videoId, browseSession, active }: StaticInspectorContext & { videoId: string }) {
  const key = JSON.stringify([channelId, videoId, browseSession]);
  const live = useRef({ key, active });
  live.current = { key, active };
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<State>({ key, kind: 'loading' });
  useSourceExpiry(state, () => setState({ key, kind: 'loading' }), () => { if (active) setRetry(value => value + 1); });
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && live.current.active && live.current.key === key;
    setState({ key, kind: 'loading' });
    loadVideoLifetimeBaseline(channelId, videoId, browseSession, controller.signal).then((payload) => {
      if (!current()) return;
      if (sourceExpired(payload)) throw new Error('Current data could not be refreshed.');
      setState({ key, kind: 'ready', payload });
    }).catch((error) => {
      if (!current() || isAbortError(error)) return;
      const unavailable = error instanceof ExplorerApiError && [403, 404, 410].includes(error.status);
      setState({ key, kind: unavailable ? 'unavailable' : 'error', message: unavailable
        ? 'Channel baseline is unavailable for this catalogue session.'
        : 'Channel baseline could not load. The existing video fields are kept.' });
    });
    return () => { controller.abort(); };
  }, [key, channelId, videoId, browseSession, active, retry]);

  const ready = active && state.key === key && state.kind === 'ready'
    && !sourceExpired(state.payload) ? state.payload : null;
  return <section className="explorer-v2-video-observation" aria-label="Channel lifetime baseline">
    <header><h3>Channel baseline</h3><span>All time · Long-form</span></header>
    {!ready ? <>
      <p role="status">{!active ? 'The catalogue changed. Reopen this video in the current results for the channel baseline.'
        : state.key !== key || state.kind === 'loading' ? 'Loading channel baseline…'
          : state.kind === 'ready' ? 'Channel baseline is unavailable.' : state.message}</p>
      {active && state.key === key && state.kind === 'error'
        && <button type="button" className="explorer-secondary-button" onClick={() => setRetry((value) => value + 1)}>Retry baseline</button>}
    </> : <>
      <p className="explorer-v2-video-observation-status">
        {formatCount(ready.sample_size, { compact: false })} {videoNoun(ready.sample_size)}{ready.status === 'preliminary' ? ' · Preliminary' : ''}
      </p>
      {ready.status === 'insufficient_data'
        ? <p role="status">At least 4 videos with available views are needed for a baseline.</p>
        : <dl className="explorer-v2-video-drawer-details">
          <div><dt>Median views</dt><dd>{formatCount(ready.median, { compact: false })}</dd></div>
          <div><dt>Typical range <span className="explorer-v2-video-observation-end-label">P25–P75</span></dt>
            <dd>{formatCount(ready.p25, { compact: false })}–{formatCount(ready.p75, { compact: false })}</dd></div>
        </dl>}
      {(ready.missing_views > 0 || ready.coverage.excluded_count > 0) && <p className="explorer-v2-video-observation-status">
        {formatCount(ready.missing_views, { compact: false })} missing views · {formatCount(ready.coverage.excluded_count, { compact: false })} excluded by data eligibility
      </p>}
      <small>Leti Cook calculation. Current accumulated views, not final views or age-adjusted performance. Independent of catalogue filters.</small>
    </>}
  </section>;
}
