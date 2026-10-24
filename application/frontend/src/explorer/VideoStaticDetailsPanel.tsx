import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired } from '../sourceLifetime.ts';
import { useEffect, useRef, useState } from 'react';
import type { VideoStaticDetailsPayload } from '../types';
import { ExplorerApiError, isAbortError, loadVideoStaticDetails } from './api';
import { formatTimestampLocal } from './localTime';

export type StaticInspectorContext = { channelId: string; browseSession: string; active: boolean };
type State = { key: string; kind: 'loading' | 'unavailable' | 'error'; message?: string }
  | { key: string; kind: 'ready'; payload: VideoStaticDetailsPayload };

export function staticInspectorRate(value: number | null): string {
  if (value === null) return 'Not available';
  // Two places; a small nonzero rate must not read as zero.
  if (value > 0 && value < 0.005) return '<0.01%';
  return `${new Intl.NumberFormat('en', { maximumFractionDigits: 2 }).format(value)}%`;
}

/** Separate, explicitly gated calculation over the Inspector's opening generation. */
export function VideoStaticDetailsPanel({ channelId, videoId, browseSession, active }: StaticInspectorContext & { videoId: string }) {
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
    loadVideoStaticDetails(channelId, videoId, browseSession, controller.signal).then((payload) => {
      if (!current()) return;
      if (sourceExpired(payload)) throw new Error('Current data could not be refreshed.');
      setState({ key, kind: 'ready', payload });
    }).catch((error) => {
      if (!current() || isAbortError(error)) return;
      const unavailable = error instanceof ExplorerApiError && [403, 404, 410].includes(error.status);
      setState({ key, kind: unavailable ? 'unavailable' : 'error',
        message: unavailable ? 'Static details are unavailable for this catalogue session.'
          : 'Static details could not load. The existing video fields are kept.' });
    });
    return () => { controller.abort(); };
  }, [key, channelId, videoId, browseSession, active, retry]);

  const ready = active && state.key === key && state.kind === 'ready'
    && !sourceExpired(state.payload) ? state.payload : null;
  if (!ready) return <section className="explorer-v2-video-observation" aria-label="Static video details">
    <header><h3>Static details</h3></header>
    <p role="status">{!active ? 'The catalogue changed. Reopen this video in the current results for static details.'
      : state.key !== key || state.kind === 'loading' ? 'Loading static details…'
        : state.kind === 'ready' ? 'Static details are unavailable.' : state.message}</p>
    {active && state.key === key && state.kind === 'error'
      && <button type="button" className="explorer-secondary-button" onClick={() => setRetry((value) => value + 1)}>Retry details</button>}
  </section>;
  return <>
    <section className="explorer-v2-video-observation" aria-label="Public interactions">
      <header><h3>Public interactions</h3><span>Leti Cook calculations</span></header>
      <dl className="explorer-v2-video-drawer-details">
        <div><dt>Like rate</dt><dd>{staticInspectorRate(ready.metrics.like_rate_percent)}</dd></div>
        <div><dt>Comment rate</dt><dd>{staticInspectorRate(ready.metrics.comment_rate_percent)}</dd></div>
      </dl>
      <small>Stored lifetime counter / views × 100. Missing inputs or zero views are unavailable.</small>
    </section>
    <section className="explorer-v2-video-observation" aria-label="Stored video metadata">
      <header><h3>Stored metadata</h3></header>
      <dl className="explorer-v2-video-drawer-details">
        <div><dt>Title length</dt><dd>{ready.metrics.title_length_characters === null ? 'Not available' : `${ready.metrics.title_length_characters} characters`}</dd></div>
        <div><dt>Tag count</dt><dd>{ready.metrics.tag_count === null ? 'Not available' : ready.metrics.tag_count}</dd></div>
        <div><dt>Metadata checked</dt><dd>{formatTimestampLocal(ready.raw.metadata_checked_at)}</dd></div>
      </dl>
      <p aria-label="Stored tags">{ready.raw.tags === null ? 'Tags not reported' : ready.raw.tags.length ? ready.raw.tags.join(' · ') : 'No tags'}</p>
      <small>Tags: stored YouTube fields. Title length and tag count: Leti Cook calculations.</small>
    </section>
  </>;
}
