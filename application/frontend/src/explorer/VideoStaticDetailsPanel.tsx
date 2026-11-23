import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired } from '../sourceLifetime.ts';
import { useEffect, useRef, useState } from 'react';
import { formatTimestampLocal } from './localTime';
import type { InspectorPanelSource, InspectorValue, StaticDetailsView } from './libraryData/inspector.ts';

type State = { key: string; kind: 'loading' | 'unavailable' | 'error' | 'inactive'; message?: string }
  | { key: string; kind: 'ready'; payload: StaticDetailsView };

export function staticInspectorRate(value: number | null): string {
  if (value === null) return 'Not available';
  // Two places; a small nonzero rate must not read as zero.
  if (value > 0 && value < 0.005) return '<0.01%';
  return `${new Intl.NumberFormat('en', { maximumFractionDigits: 2 }).format(value)}%`;
}

const NEEDS_REFRESH = 'Needs refresh';
/** An expired value is "—" with "Needs refresh"; unknown or unavailable keep their own wording. */
function shown<T>(value: InspectorValue<T>, known: (item: T) => string | number, missing = 'Not available') {
  return value.state === 'known' ? { text: known(value.value) }
    : value.state === 'expired' ? { text: '—', title: NEEDS_REFRESH } : { text: missing };
}

/** Separate, explicitly gated calculation over the Inspector's opening generation. */
export function VideoStaticDetailsPanel({ source }: { source: InspectorPanelSource<StaticDetailsView> }) {
  const { key, active, pending } = source;
  const live = useRef({ key, active });
  live.current = { key, active };
  const reader = useRef(source);
  reader.current = source;
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<State>({ key, kind: 'loading' });
  useSourceExpiry(state, () => setState({ key, kind: 'loading' }), () => { if (active) setRetry(value => value + 1); });
  useEffect(() => {
    if (!active || pending) return;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && live.current.active && live.current.key === key;
    setState({ key, kind: 'loading' });
    reader.current.load(controller.signal).then((payload) => {
      if (!current()) return;
      if (sourceExpired(payload)) throw new Error('Current data could not be refreshed.');
      setState({ key, kind: 'ready', payload });
    }).catch((error) => {
      if (!current()) return;
      const failure = reader.current.failure(error);
      if (!failure) return;
      setState({ key, kind: failure.kind, message: failure.kind === 'error'
        ? 'Static details could not load. The existing video fields are kept.'
        : failure.notInPopulation ? 'Static details are unavailable: this video is not in the current results.'
          : reader.current.mode === 'query' ? 'Static details are unavailable for this video.'
            : 'Static details are unavailable for this catalogue session.' });
    });
    return () => { controller.abort(); };
  }, [key, active, pending, retry]);

  const ready = active && state.key === key && state.kind === 'ready'
    && !sourceExpired(state.payload) ? state.payload : null;
  if (!ready) return <section className="explorer-v2-video-observation" aria-label="Static video details">
    <header><h3>Static details</h3></header>
    <p role="status">{!active || (state.key === key && state.kind === 'inactive') ? 'The catalogue changed. Reopen this video in the current results for static details.'
      : pending || state.key !== key || state.kind === 'loading' ? 'Loading static details…'
        : state.kind === 'ready' ? 'Static details are unavailable.' : state.message}</p>
    {active && state.key === key && state.kind === 'error'
      && <button type="button" className="explorer-secondary-button" onClick={() => setRetry((value) => value + 1)}>Retry details</button>}
  </section>;
  const likeRate = shown(ready.like_rate, (value) => staticInspectorRate(Number(value)));
  const commentRate = shown(ready.comment_rate, (value) => staticInspectorRate(Number(value)));
  const titleLength = shown(ready.title_length, (value) => `${value} characters`);
  const tagCount = shown(ready.tag_count, (value) => value);
  const tags = shown(ready.tags, (value) => (value.length ? value.join(' · ') : 'No tags'), 'Tags not reported');
  const description = ready.description;
  return <>
    <section className="explorer-v2-video-observation" aria-label="Public interactions">
      <header><h3>Public interactions</h3><span>Leti Cook calculations</span></header>
      <dl className="explorer-v2-video-drawer-details">
        <div><dt>Like rate</dt><dd title={likeRate.title}>{likeRate.text}</dd></div>
        <div><dt>Comment rate</dt><dd title={commentRate.title}>{commentRate.text}</dd></div>
      </dl>
      <small>Stored lifetime counter / views × 100. Missing inputs or zero views are unavailable.</small>
    </section>
    <section className="explorer-v2-video-observation" aria-label="Stored video metadata">
      <header><h3>Stored metadata</h3></header>
      <dl className="explorer-v2-video-drawer-details">
        <div><dt>Title length</dt><dd title={titleLength.title}>{titleLength.text}</dd></div>
        <div><dt>Tag count</dt><dd title={tagCount.title}>{tagCount.text}</dd></div>
        <div><dt>Metadata checked</dt><dd>{formatTimestampLocal(ready.metadata_checked_at)}</dd></div>
      </dl>
      <p aria-label="Stored tags" title={tags.title}>{tags.text}</p>
      <small>Tags: stored YouTube fields. Title length and tag count: Leti Cook calculations.</small>
    </section>
    {description && <section className="explorer-v2-video-observation" aria-label="Video description">
      <header><h3>Description</h3><span>Stored YouTube field</span></header>
      {description.state === 'known' && description.value
        ? <p className="explorer-v2-video-description" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{description.value}</p>
        : <p role="status" title={description.state === 'expired' ? NEEDS_REFRESH : undefined}>
          {description.state === 'known' ? 'No description.' : description.state === 'expired' ? '—'
            : description.state === 'unknown' ? 'Description not reported.' : 'Description unavailable.'}</p>}
    </section>}
  </>;
}
