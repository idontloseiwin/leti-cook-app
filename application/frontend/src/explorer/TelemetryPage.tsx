import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ArrowRight, ChevronDown, ChevronRight, Clock3, Pause, Play, Radio, RefreshCw, Search, X } from 'lucide-react';
import type { TelemetryEvent } from '../types';
import { useOperationalRead } from './useOperationalRead';
import { useTelemetryStream } from './useTelemetryStream';
import { TelemetryFlowMapChart } from './TelemetryFlowMapChart';
import { plainServerText } from '../serverText';
import {
  eventIdentity, eventTimestamp, filterTelemetryEvents, loadTelemetryRecent,
  telemetryDuration, telemetryFailed, telemetryFlows, telemetrySourceLabel, telemetryTime, telemetrySummaryText,
  telemetryExact, telemetryDate, telemetryRunContext, telemetryRunDuration, telemetryRunExplanation, telemetryWaitingPublications, telemetryCountLabels, loadTelemetrySystem, TELEMETRY_VIEW_LIMIT,
  telemetryRunPhases, telemetryVideoObservation,
} from './telemetryWorkspace';
import './telemetry.css';

const integer = (value: number) => value.toLocaleString('en');

function EventDetails({ event, onClose }: { event: TelemetryEvent; onClose: () => void }) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => { panel.current?.focus(); }, [event.id]);
  const number = (value: unknown) => typeof value === 'number' ? integer(value) : 'Not recorded';
  const countLabels = telemetryCountLabels(event);
  const observation = telemetryVideoObservation(event);
  return <aside className="telemetry-v2-inspector" aria-label="Telemetry event details" ref={panel} tabIndex={-1}>
    <header><div><p className="telemetry-v2-eyebrow">Recorded event</p><h3>{event.operation}</h3></div>
      <button type="button" onClick={onClose} aria-label="Close event details"><X size={16} /></button></header>
    <p className="telemetry-v2-detail-route">{telemetrySourceLabel(event.source!)} <ArrowRight size={15} aria-hidden="true" /> {telemetrySourceLabel(event.target!)}</p>
    <dl>
      <div><dt>Observed at</dt><dd>{telemetryExact(event.ts)}</dd></div>
      <div><dt>Kind</dt><dd>{event.kind}</dd></div>
      <div><dt>Status</dt><dd className={telemetryFailed(event) ? 'is-error' : undefined}>{event.status || 'Not recorded'}</dd></div>
      <div><dt>Direction</dt><dd>{event.direction || 'Not recorded'}</dd></div>
      <div><dt>{event.operation === 'video_validation' ? 'Rejected field' : 'Phase'}</dt><dd>{event.phase || 'Not recorded'}</dd></div>
      <div><dt>Reason</dt><dd>{telemetrySummaryText(event.reason) || 'Not recorded'}</dd></div>
      {event.video_id !== undefined && <div><dt>Video ID</dt><dd>{telemetrySummaryText(event.video_id) || 'Not recorded'}</dd></div>}
      {event.channel_id !== undefined && <div><dt>Channel ID</dt><dd>{telemetrySummaryText(event.channel_id) || 'Not recorded'}</dd></div>}
      {event.batch_id !== undefined && <div><dt>Batch ID</dt><dd>{telemetrySummaryText(event.batch_id) || 'Not recorded'}</dd></div>}
      {event.sync_run_id !== undefined && <div><dt>Sync run</dt><dd>{number(event.sync_run_id)}</dd></div>}
      {event.endpoint !== undefined && <div><dt>API endpoint</dt><dd>{telemetrySummaryText(event.endpoint) || 'Not recorded'}</dd></div>}
      {event.requested_count !== undefined && <div><dt>{countLabels.requested}</dt><dd>{number(event.requested_count)}</dd></div>}
      {event.received_count !== undefined && <div><dt>{countLabels.received}</dt><dd>{number(event.received_count)}</dd></div>}
      {event.operation === 'video_provider_manifest' && Array.isArray(event.video_ids) && <div><dt>Returned requested IDs</dt><dd>{event.video_ids.filter((id): id is string => typeof id === 'string' && /^[A-Za-z0-9_-]{11}$/.test(id)).slice(0, 50).join(', ') || 'None'}</dd></div>}
      {event.retry_after_seconds !== undefined && <div><dt>Requested retry delay (seconds)</dt><dd>{number(event.retry_after_seconds)}</dd></div>}
      <div><dt>Duration</dt><dd>{telemetryDuration(event.duration_ms)}</dd></div>
      <div><dt>{countLabels.count ?? 'Count'}</dt><dd>{number(event.count)}</dd></div>
      <div><dt>Reported quota units</dt><dd>{number(event.quota)}</dd></div>
      <div><dt>HTTP status</dt><dd>{number(event.http_status)}</dd></div>
    </dl>
    {observation && <p className="telemetry-v2-note">{observation}</p>}
    {event.detail && <div className="telemetry-v2-event-note"><h4>Detail</h4><p>{plainServerText(event.detail, 640) ?? 'Withheld: the detail looked like a URL, a token or a stack trace.'}</p></div>}
    <details><summary>Event identity</summary><code>{event.id}</code><p>Envelope v{number(event.schema_version ?? 1)} · source: {event.source} · target: {event.target}</p></details>
    <p className="telemetry-v2-note">One diagnostic event, not an incident or a complete request trace. Counts keep the producer's meaning; quota is not a remaining balance.</p>
  </aside>;
}

export function TelemetryPage() {
  const [paused, setPaused] = useState(false);
  const [revision, setRevision] = useState(0);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('');
  const [failedOnly, setFailedOnly] = useState(false);
  const [flowKey, setFlowKey] = useState<string>();
  const [selected, setSelected] = useState<TelemetryEvent>();
  const searchInput = useRef<HTMLInputElement>(null);
  const selectionTrigger = useRef<HTMLButtonElement>(null);
  const [clock, setClock] = useState(Date.now());
  const recent = useOperationalRead(loadTelemetryRecent, { intervalMs: 30_000, enabled: !paused, errorMessage: 'Recent events could not be refreshed. Previously received events are kept.' });
  const system = useOperationalRead(loadTelemetrySystem, { intervalMs: 60_000, errorMessage: 'Run context could not be refreshed. Telemetry remains independent.' });
  const stream = useTelemetryStream(recent.payload, paused, revision);
  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 15_000); return () => window.clearInterval(timer); }, []);
  useEffect(() => {
    if (!selected) return;
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') closeDetails(); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [selected]);
  const events = stream.events;
  const flows = useMemo(() => telemetryFlows(events), [events]);
  const kinds = useMemo(() => [...new Set(events.map((event) => event.kind!))].sort(), [events]);
  const visible = useMemo(() => filterTelemetryEvents(events, { query, kind, failedOnly, flowKey }).reverse(), [events, query, kind, failedOnly, flowKey]);
  const latest = events[events.length - 1];
  const quiet = !latest || clock - eventTimestamp(latest) > 60_000;
  const journalUnavailable = stream.journalStatus === 'unavailable';
  const status = paused ? 'Paused' : journalUnavailable && stream.connection === 'connected' ? 'Journal unavailable' : stream.connection === 'connected' ? quiet ? 'Connected · quiet' : 'Connected' : {
    connecting: 'Connecting', reconnecting: 'Reconnecting', unavailable: 'Stream unavailable',
  }[stream.connection];
  const statusTone = paused ? 'paused' : stream.connection === 'connected' && !journalUnavailable ? 'connected' : 'warning';
  const selectedFlow = flows.find((flow) => flow.key === flowKey);
  const errorCount = events.filter(telemetryFailed).length;
  const rejected = stream.rejected + (recent.payload?.rejected ?? 0);
  const runContext = telemetryRunContext(system.payload);
  const summaryStale = Boolean(system.error || (system.updatedAt && clock - system.updatedAt > 90_000));
  const resetFilters = () => { setQuery(''); setKind(''); setFailedOnly(false); setFlowKey(undefined); };
  const retryStream = () => { setRevision((value) => value + 1); recent.refresh(); };
  const hasFilters = Boolean(query || kind || failedOnly || flowKey);
  function closeDetails() {
    setSelected(undefined);
    if (selectionTrigger.current?.isConnected) selectionTrigger.current.focus();
    else searchInput.current?.focus();
  }

  return <main className="telemetry-v2-workspace" aria-labelledby="telemetry-v2-title">
    <header className="telemetry-v2-page-heading">
      <div><p className="telemetry-v2-eyebrow">Operations</p><h1 id="telemetry-v2-title">Telemetry</h1>
        <p>Follow activity. Inspect an event. Review a run.</p></div>
      <div className="telemetry-v2-actions">
        <span className={`telemetry-v2-connection is-${statusTone}`} role="status"><Radio size={14} aria-hidden="true" />{status}</span>
        <button type="button" onClick={() => setPaused((value) => !value)}>{paused ? <Play size={15} /> : <Pause size={15} />}{paused ? 'Resume' : 'Pause'}</button>
        {!paused && <button type="button" onClick={retryStream} aria-label="Reconnect telemetry and refresh diagnostic reads"><RefreshCw size={15} />Reconnect</button>}
      </div>
    </header>

    <div className="telemetry-v2-metrics" aria-label="Retained event sample">
      <div><span>Retained events</span><strong>{integer(events.length)}<small> / {TELEMETRY_VIEW_LIMIT}</small></strong></div>
      <div><span>Observed routes</span><strong>{integer(flows.length)}</strong></div>
      <div><span>Error events in sample</span><strong className={errorCount ? 'is-error' : undefined}>{integer(errorCount)}</strong></div>
      <div className="telemetry-v2-last-event"><span>Latest event</span><strong>{latest ? telemetryTime(latest.ts) : 'No events yet'}</strong><small>{latest ? telemetryDate(latest.ts) : 'Waiting for a recorded observation'}</small></div>
    </div>

    {(paused || recent.error || journalUnavailable || rejected > 0) && <div className="telemetry-v2-notices" aria-live="polite">
      {paused && <p>Event updates are paused in this browser. Collection continues; resume requests the server's bounded replay.</p>}
      {recent.error && <p className="is-warning">{recent.error}</p>}
      {journalUnavailable && !paused && <p className="is-warning">The event journal cannot currently be read. Previously received events are kept; collection status is shown separately in Run context.</p>}
      {rejected > 0 && <p className="is-warning">Some diagnostic messages did not match the supported telemetry contract and were not displayed.</p>}
    </div>}

    <TelemetryFlowMapChart events={events} arrivals={stream.arrivals} paused={paused} connected={stream.connection === 'connected'} journalUnavailable={journalUnavailable} selectedRoute={flowKey}
      onSelectRoute={(key) => setFlowKey((current) => current === key ? undefined : key)} />
    <div className="telemetry-v2-workbench"><div className="telemetry-v2-primary">
    <section className="telemetry-v2-flow-section" aria-labelledby="telemetry-v2-flow-title">
      <div className="telemetry-v2-section-heading"><div><p className="telemetry-v2-eyebrow">Event sample</p><h2 id="telemetry-v2-flow-title">Observed activity</h2></div><span>{Math.min(flows.length, 8)} of {flows.length} routes</span></div>
      <div className="telemetry-v2-flows">
        {flows.slice(0, 8).map((flow) => <button className={`telemetry-v2-flow ${flowKey === flow.key ? 'is-selected' : ''}`} key={flow.key} type="button"
          onClick={() => setFlowKey((value) => value === flow.key ? undefined : flow.key)} aria-pressed={flowKey === flow.key}>
          <span className="telemetry-v2-flow-route" title={`${flow.source} → ${flow.target}`}><span>{telemetrySourceLabel(flow.source)}</span><ArrowRight size={14} aria-hidden="true" /><span>{telemetrySourceLabel(flow.target)}</span></span>
          <span className="telemetry-v2-flow-count">{integer(flow.count)} events{flow.errors > 0 && <small className="is-error">{flow.errors} error</small>}</span>
          <span className="telemetry-v2-flow-meter" aria-hidden="true"><span style={{ width: `${100 * flow.count / Math.max(1, events.length)}%` }} /></span>
        </button>)}
        {!flows.length && <p className="telemetry-v2-empty">{recent.loading ? 'Loading recent events…' : stream.connection === 'connected' && !journalUnavailable ? 'No events in the available replay. Quiet is not a pipeline failure.' : 'No events received yet. The connection status is shown above.'}</p>}
      </div>
      <p className="telemetry-v2-note">Select a route to filter events. Source → target as recorded; bars show share of this sample, not throughput.</p>
    </section>

    <section className="telemetry-v2-events-section" aria-labelledby="telemetry-v2-events-title">
      <div className="telemetry-v2-section-heading"><h2 id="telemetry-v2-events-title">Event log</h2><span>{visible.length} of {events.length} retained events</span></div>
      <div className="telemetry-v2-toolbar">
        <label className="telemetry-v2-search"><Search size={16} aria-hidden="true" /><input ref={searchInput} type="search" value={query} maxLength={160} onChange={(event) => setQuery(event.target.value)} placeholder="Search operation, reason or Video ID" aria-label="Search telemetry events" /></label>
        <label className="telemetry-v2-kind"><span>Kind</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="">All kinds</option>{[...new Set([...kinds, ...(kind ? [kind] : [])])].map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="telemetry-v2-errors"><input type="checkbox" checked={failedOnly} onChange={(event) => setFailedOnly(event.target.checked)} />Errors only</label>
        {hasFilters && <button type="button" onClick={resetFilters}>Clear filters</button>}
      </div>
      {flowKey && <div className="telemetry-v2-filter-chip"><span>{selectedFlow ? `${telemetrySourceLabel(selectedFlow.source)} → ${telemetrySourceLabel(selectedFlow.target)}` : 'Selected route no longer in sample'}</span><button type="button" onClick={() => setFlowKey(undefined)} aria-label="Remove route filter"><X size={14} /></button></div>}
      <div className={`telemetry-v2-events-layout ${selected ? 'has-inspector' : ''}`}>
        <div className="telemetry-v2-event-list" aria-label="Newest events first">
          {visible.map((event) => <button className={`telemetry-v2-event ${telemetryFailed(event) ? 'is-failed' : ''} ${selected?.id === event.id ? 'is-selected' : ''}`}
            type="button" key={eventIdentity(event)} onClick={(click) => { selectionTrigger.current = click.currentTarget; setSelected(event); }} aria-pressed={selected?.id === event.id}>
            <span className="telemetry-v2-event-main"><strong>{event.operation}</strong><span>{telemetrySourceLabel(event.source!)} → {telemetrySourceLabel(event.target!)}</span></span>
            <span className="telemetry-v2-event-status"><b>{event.status || event.kind}</b><small>{event.direction || event.kind}{typeof event.duration_ms === 'number' ? ` · ${telemetryDuration(event.duration_ms)}` : ''}</small></span>
            <time title={telemetryExact(event.ts)} dateTime={event.ts}>{telemetryTime(event.ts)}</time><ChevronRight size={14} aria-hidden="true" />
          </button>)}
          {!visible.length && <div className="telemetry-v2-empty">{hasFilters ? 'No retained events match these filters.' : recent.loading ? 'Loading recent events…' : 'No events to show yet.'}</div>}
        </div>
        {selected && <EventDetails event={selected} onClose={closeDetails} />}
      </div>
      <p className="telemetry-v2-note">Newest first · times in your time zone · at most {TELEMETRY_VIEW_LIMIT} retained events. Best-effort delivery and bounded replay can omit events; this is not a complete history.</p>
    </section>
    </div>
    <div className="telemetry-v2-context-grid">
      <section className="telemetry-v2-context" aria-labelledby="telemetry-v2-run-title"><div className="telemetry-v2-section-heading"><h2 id="telemetry-v2-run-title"><Activity size={16} />Run context</h2><button type="button" onClick={system.refresh} disabled={system.loading || system.refreshing} aria-label="Refresh run context"><RefreshCw size={14} /></button></div>
        {system.error && <p className="is-warning" role="status">{system.error}</p>}
        {system.payload && <div className={`telemetry-v2-context-status ${summaryStale ? 'is-warning' : ''}`}><Clock3 size={15} />{summaryStale ? 'Last received · stale' : 'Recorded, not live'}</div>}
        {system.payload ? <dl>
          <div><dt>Latest run status</dt><dd>{runContext.status || 'Not recorded'}{system.error ? ' · last received' : ''}</dd></div>
          <div><dt>Last recorded phase</dt><dd>{runContext.phase || 'Not recorded'}</dd></div>
          <div><dt>Run started</dt><dd>{telemetryExact(runContext.startedAt)}</dd></div>
          <div><dt>Progress updated</dt><dd>{telemetryExact(runContext.progressAt)}</dd></div>
          <div><dt>Run finished</dt><dd>{telemetryExact(runContext.finishedAt)}</dd></div>
          <div><dt>Summary timestamp</dt><dd>{telemetryExact(runContext.summaryAt)}</dd></div>
        </dl> : !system.error && <p className="telemetry-v2-note">{system.loading ? 'Loading run context…' : 'Run context unavailable.'}</p>}
        <p className="telemetry-v2-note">Separate backend summary, refreshed every 60 seconds. Events are not used to infer run completion or queue state.</p>
      </section>
    </div>
    </div>

    <section className="telemetry-v2-history" aria-labelledby="telemetry-v2-history-title">
      <div className="telemetry-v2-section-heading"><div><p className="telemetry-v2-eyebrow">Backend records</p><h2 id="telemetry-v2-history-title">Sync history</h2></div>
        <span>{system.payload ? `${system.payload.recent_runs.length} latest runs${summaryStale ? ' · last received' : ''}` : 'Up to 10 latest runs'}</span></div>
      {system.error && <p className="is-warning" role="status">Run history could not be refreshed. Previously received records are kept.</p>}
      {system.payload?.recent_runs.map((run, index) => <details className="telemetry-v2-run" key={`${run.id ?? run.started_at ?? 'run'}-${index}`}>
        <summary><span className="telemetry-v2-run-date"><time dateTime={run.started_at}>{telemetryDate(run.started_at)}</time><small>{telemetryTime(run.started_at)}</small></span>
          <span className={`telemetry-v2-run-status ${['error', 'failed', 'partial'].includes(run.status ?? '') ? 'is-warning' : ''}`}>{run.status || 'Not recorded'}</span>
          <span className="telemetry-v2-run-count">{run.stats_updated === undefined ? 'Updates not recorded' : `${integer(run.stats_updated)} stats updated`}{(run.collection?.unresolved ?? 0) > 0 ? ` · ${integer(run.collection!.unresolved!)} unfinished in this run` : ''}</span>
          <span className="telemetry-v2-run-duration">{telemetryRunDuration(run.duration_seconds)}<ChevronDown size={15} /></span></summary>
        {telemetryRunExplanation(run) && <p className="telemetry-v2-note">{telemetryRunExplanation(run)}</p>}
        <dl><div><dt>Started</dt><dd>{telemetryExact(run.started_at)}</dd></div><div><dt>Finished</dt><dd>{telemetryExact(run.finished_at)}</dd></div>
          <div><dt>Last recorded phase</dt><dd>{run.current_phase || run.phase_state || 'Not recorded'}</dd></div>
          <div><dt>New videos</dt><dd>{run.new_videos === undefined ? 'Not recorded' : integer(run.new_videos)}</dd></div>
          <div><dt>Recorded errors</dt><dd>{run.errors === undefined ? 'Not recorded' : integer(run.errors)}</dd></div>
          {run.collection?.claimed !== undefined && <div><dt>Videos claimed for collection</dt><dd>{integer(run.collection.claimed)}</dd></div>}
          {run.collection?.unavailable_marked !== undefined && <div><dt>Videos recorded as unavailable via API</dt><dd>{integer(run.collection.unavailable_marked)}</dd></div>}
          {telemetryWaitingPublications(run.collection) !== undefined && <div><dt>Waiting publications handled</dt><dd>{integer(telemetryWaitingPublications(run.collection)!)}</dd></div>}
        </dl>
        <h3>Recorded phase durations</h3>
        <dl>{telemetryRunPhases.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{telemetryRunDuration(run.timings?.[key])}</dd></div>)}</dl>
        <p className="telemetry-v2-note">Phase durations include waits inside each phase. Missing measurements are not zero; older runs may only have a total duration.</p>
      </details>)}
      {!system.payload?.recent_runs.length && <p className="telemetry-v2-empty">{system.loading ? 'Loading run history…' : system.payload ? 'No run records returned.' : 'Run history unavailable.'}</p>}
      <p className="telemetry-v2-note">Stored runs, refreshed every 60 seconds. Counts describe each recorded run, not the current queue. Next checks follow the queue's current deadlines.</p>
    </section>
  </main>;
}
